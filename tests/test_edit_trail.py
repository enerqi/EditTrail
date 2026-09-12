"""Behaviour tests for edit_trail against the fake Sublime API in conftest.py."""

from __future__ import annotations

import os.path
from typing import TYPE_CHECKING

import pytest

import edit_trail
from tests.conftest import BODY, FakeView, FakeWindow, plugin_settings, status_messages

if TYPE_CHECKING:
    from pathlib import Path

CASE_INSENSITIVE_PATHS = os.path.normcase("A") == "a"


def _history(window: FakeWindow) -> edit_trail.History:
    return edit_trail._State.histories[window.id()]


def _files(tmp_path: Path, *names: str) -> list[str]:
    paths: list[str] = []
    for name in names:
        path = tmp_path / name
        path.write_text(BODY)
        paths.append(str(path))
    return paths


def _rows(history: edit_trail.History) -> list[tuple[str | None, int]]:
    """(file name, current row) per entry, for readable assertions.

    An attached entry has no file_name of its own: it is read from the view on detach.
    """
    result: list[tuple[str | None, int]] = []
    for entry in history.entries:
        view, point = entry.live_view(), entry.point()
        if view is not None and point is not None:
            result.append((view.file_name(), view.rowcol(point)[0]))
        else:
            result.append((entry.file_name, entry.row))
    return result


# Recording


def test_nearby_edits_merge_into_one_entry(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(10, "x")
    a.type_at_row(12, "y")
    a.type_at_row(15, "z")
    assert _rows(_history(window)) == [(fa, 15)]


def test_far_edits_and_other_files_add_entries(window: FakeWindow, tmp_path: Path) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, BODY), FakeView(window, fb, BODY)
    a.type_at_row(10, "x")
    a.type_at_row(50, "y")
    b.type_at_row(5, "z")
    assert _rows(_history(window)) == [(fa, 10), (fa, 50), (fb, 5)]


def test_zero_merge_distance_only_merges_same_line(window: FakeWindow, tmp_path: Path) -> None:
    plugin_settings.set("merge_line_distance", 0)
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(10, "x")
    a.type_at_row(10, "y")
    a.type_at_row(11, "z")
    assert _rows(_history(window)) == [(fa, 10), (fa, 11)]


def test_positions_follow_text_inserted_above(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(50, "x")
    a.type_at_row(0, "new\nnew\n")
    assert _rows(_history(window)) == [(fa, 52), (fa, 2)]


def test_scratch_and_panel_views_are_ignored(window: FakeWindow) -> None:
    FakeView(window, None, "", scratch=True).type(0, "terminal output")
    FakeView(window, None, "", element="output:exec").type(0, "build output")
    assert window.id() not in edit_trail._State.histories


def test_widget_views_are_ignored(window: FakeWindow) -> None:
    FakeView(window, None, "", widget=True).type(0, "find what")
    assert window.id() not in edit_trail._State.histories


def test_a_view_turned_scratch_after_an_edit_stops_being_recorded(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(10, "x")
    a.scratch = True  # e.g. a plugin takes the buffer over to render a preview
    a.type_at_row(50, "y")
    assert [row for _, row in _rows(_history(window))] == [10]


def test_files_loading_or_restored_with_the_project_are_not_edits(window: FakeWindow, tmp_path: Path) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    FakeView(window, fa).external_change(0, BODY, loading=True)
    FakeView(window, fb).external_change(0, BODY)
    assert window.id() not in edit_trail._State.histories


def test_reload_from_disk_after_edits_adds_no_entry(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(10, "x")
    a.cursor = a.text_point(80, 0)
    a.external_change(0, "changed on disk\n")
    assert _rows(_history(window)) == [(fa, 11)]


def test_changes_in_background_views_are_not_edits(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, BODY), FakeView(window, fb, BODY)
    a.type_at_row(10, "x")
    b.dirty = True  # e.g. whitespace trimmed by save-all while a stays focused
    listener.on_modified(b)
    assert _rows(_history(window)) == [(fa, 10)]


def test_debug_setting_prints_decisions(window: FakeWindow, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plugin_settings.set("debug", True)
    (fa,) = _files(tmp_path, "a.txt")
    view = FakeView(window, fa)
    view.external_change(0, BODY)
    view.type_at_row(3, "x")
    out = capsys.readouterr().out
    assert "ignored change with no unsaved edits" in out
    assert "recorded edit at line 4" in out


def test_modification_with_no_selection_is_ignored(
    window: FakeWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    view = FakeView(window, fa, BODY)
    monkeypatch.setattr(view, "sel", list)  # an empty Selection raises IndexError when indexed
    view.type_at_row(3, "x")
    assert window.id() not in edit_trail._State.histories

    edit_trail._navigate(window, -1)  # no cursor to compare against either
    assert status_messages[-1] == "EditTrail: no edit locations recorded"


def test_max_entries_caps_history_and_erases_dropped_regions(window: FakeWindow, tmp_path: Path) -> None:
    plugin_settings.set("max_entries", 3)
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    for row in range(0, 100, 10):
        a.type_at_row(row, "e")
    assert [row for _, row in _rows(_history(window))] == [70, 80, 90]
    assert len(a.regions) == 3


def test_lowering_max_entries_trims_the_existing_history(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    for row in range(0, 100, 10):
        a.type_at_row(row, "e")
    history = _history(window)
    assert len(history.entries) == 10

    plugin_settings.set("max_entries", 2)
    assert [row for _, row in _rows(history)] == [80, 90]
    assert len(a.regions) == 2
    assert history.index == 2


def test_invalid_settings_fall_back_to_defaults() -> None:
    plugin_settings.set("max_entries", "lots")
    plugin_settings.set("merge_line_distance", -1)
    assert edit_trail._Config.max_entries == edit_trail.DEFAULT_MAX_ENTRIES
    assert edit_trail._Config.merge_line_distance == edit_trail.DEFAULT_MERGE_LINE_DISTANCE


def test_a_boolean_is_not_a_valid_count() -> None:
    plugin_settings.set("max_entries", True)  # a plausible typo in the settings file
    assert edit_trail._Config.max_entries == edit_trail.DEFAULT_MAX_ENTRIES


# Navigation


def test_back_and_forward_walk_the_history(window: FakeWindow, tmp_path: Path) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, BODY), FakeView(window, fb, BODY)
    a.type_at_row(10, "x")
    b.type_at_row(20, "y")
    a.type_at_row(60, "z")
    window.active = a  # cursor sits on the newest edit

    edit_trail._navigate(window, -1)  # skips the newest, since the cursor is already there
    assert window.active is b
    assert b.cursor_row() == 20
    edit_trail._navigate(window, -1)
    assert window.active is a
    assert a.cursor_row() == 10
    edit_trail._navigate(window, -1)
    assert status_messages[-1] == "EditTrail: no older edit locations"

    edit_trail._navigate(window, 1)
    assert window.active is b
    edit_trail._navigate(window, 1)
    assert window.active is a
    assert a.cursor_row() == 60
    edit_trail._navigate(window, 1)
    assert status_messages[-1] == "EditTrail: no newer edit locations"


def test_back_does_not_skip_newest_when_cursor_is_elsewhere(window: FakeWindow, tmp_path: Path) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, BODY), FakeView(window, fb, BODY)
    a.type_at_row(10, "x")
    b.type_at_row(20, "y")
    window.active = a
    edit_trail._navigate(window, -1)
    assert window.active is b


def test_new_edit_returns_to_head_without_truncating(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    for row in (10, 30, 50):
        a.type_at_row(row, "x")
    window.active = a
    edit_trail._navigate(window, -1)
    edit_trail._navigate(window, -1)
    a.type_at_row(90, "k")
    history = _history(window)
    assert history.index == len(history.entries) == 4


def test_entries_collapsed_onto_one_spot_are_visited_once(window: FakeWindow, tmp_path: Path) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, BODY), FakeView(window, fb, BODY)
    a.type_at_row(10, "x")
    a.type_at_row(30, "y")
    b.type_at_row(5, "z")
    older, collapsed, _ = _history(window).entries
    a.regions[collapsed.key] = a.regions[older.key]  # the text between them was deleted

    edit_trail._navigate(window, -1)  # skips b (cursor is there), shows the collapsed spot
    assert window.active is a
    assert a.cursor_row() == 10
    edit_trail._navigate(window, -1)  # the older entry is the same spot: skipped
    assert status_messages[-1] == "EditTrail: no older edit locations"
    assert _history(window).index == 1


def test_commands_are_enabled_only_when_there_is_somewhere_to_go(window: FakeWindow, tmp_path: Path) -> None:
    back = edit_trail.EditTrailBackCommand(window)
    forward = edit_trail.EditTrailForwardCommand(window)
    assert not back.is_enabled()
    assert not forward.is_enabled()

    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(10, "x")
    a.type_at_row(50, "y")
    assert back.is_enabled()
    assert not forward.is_enabled()

    back.run()
    assert _history(window).index == 0
    assert not back.is_enabled()
    assert forward.is_enabled()


def test_navigating_with_no_history_reports_it(window: FakeWindow) -> None:
    edit_trail._navigate(window, -1)
    assert status_messages[-1] == "EditTrail: no edit locations recorded"


# Closing and reopening


def test_closed_file_reopens_at_remembered_position_and_reattaches(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, BODY), FakeView(window, fb, BODY)
    b.type_at_row(40, "k")
    a.type_at_row(5, "x")
    b.close()
    entry = _history(window).entries[0]
    assert entry.view is None
    assert (entry.file_name, entry.row) == (fb, 40)

    window.active = a
    edit_trail._navigate(window, -1)
    assert window.opened == [f"{fb}:41:2"]  # 1-based row, cursor after the typed "k"

    reopened = FakeView(window, fb, b.buf.text)
    listener.on_load(reopened)
    assert entry.live_view() is reopened
    assert _rows(_history(window))[0] == (fb, 40)


def test_reattach_clamps_to_a_file_that_shrank(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(90, "x")
    a.close()
    shorter = FakeView(window, fa, "only\ntwo lines")
    listener.on_load(shorter)
    point = _history(window).entries[0].point()
    assert point is not None
    assert 0 <= point <= shorter.size()


def test_revert_reanchors_entries_whose_region_was_dropped(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(90, "x")
    entry = _history(window).entries[0]
    a.revert("only\ntwo lines", keep_regions=False)
    point = entry.point()
    assert point is not None
    assert 0 <= point <= a.size()


def test_reload_refreshes_the_fallback_row_from_a_surviving_region(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(50, "x")
    a.type_at_row(0, "new\nnew\n")  # the first entry's region slides down to row 52
    entry = _history(window).entries[0]
    assert entry.row == 50  # not refreshed since the entry was created

    a.reload_from_disk(a.buf.text)
    assert entry.row == 52


@pytest.mark.skipif(not CASE_INSENSITIVE_PATHS, reason="paths are case-sensitive on this platform")
def test_reopening_with_a_differently_cased_path_reattaches(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(10, "x")
    a.close()

    reopened = FakeView(window, fa.upper(), BODY)
    listener.on_load(reopened)
    assert _history(window).entries[0].live_view() is reopened


def test_entries_for_deleted_files_are_dropped_and_skipped(window: FakeWindow, tmp_path: Path) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, BODY), FakeView(window, fb, BODY)
    a.type_at_row(10, "x")
    b.type_at_row(20, "y")
    b.close()
    (tmp_path / "b.txt").unlink()

    window.active = None
    edit_trail._navigate(window, -1)
    assert window.active is a
    assert window.opened == []
    assert [name for name, _ in _rows(_history(window))] == [fa]


def test_unsaved_buffer_entries_move_to_clone_then_drop(window: FakeWindow) -> None:
    original = FakeView(window, None, BODY)
    original.type_at_row(3, "u")
    clone = FakeView(window, None, buffer=original.buf)
    original.close()
    history = _history(window)
    assert history.entries[0].live_view() is clone
    assert clone.get_regions(history.entries[0].key)

    clone.close()
    assert history.entries == []


def test_buffer_saved_after_edit_is_reopenable(window: FakeWindow, tmp_path: Path) -> None:
    view = FakeView(window, None, BODY)
    view.type_at_row(7, "x")
    (path,) = _files(tmp_path, "saved.txt")
    view.fname = path  # simulates Save As
    view.close()
    entry = _history(window).entries[0]
    assert entry.file_name == path
    assert entry.is_reachable()


# Window isolation


def _move_to(view: FakeView, window: FakeWindow) -> None:
    """Simulate dragging a tab into another window."""
    view.win._views.remove(view)
    view.win = window
    window._views.append(view)


def test_navigation_never_follows_a_tab_dragged_to_another_window(window: FakeWindow, tmp_path: Path) -> None:
    other = FakeWindow()
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, BODY), FakeView(window, fb, BODY)
    a.type_at_row(10, "x")
    b.type_at_row(20, "y")
    _move_to(b, other)
    window.active = a

    edit_trail._navigate(window, -1)
    assert other.active is None
    assert window.active is a
    assert a.cursor_row() == 10
    assert [name for name, _ in _rows(_history(window))] == [fa]


def test_reopening_a_file_in_another_window_does_not_take_its_entries(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    other = FakeWindow()
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(10, "x")
    a.close()

    elsewhere = FakeView(other, fa, BODY)
    listener.on_load(elsewhere)
    assert _history(window).entries[0].view is None

    here = FakeView(window, fa, BODY)
    listener.on_load(here)
    assert _history(window).entries[0].live_view() is here


def test_unsaved_buffer_entries_do_not_move_to_a_clone_in_another_window(window: FakeWindow) -> None:
    other = FakeWindow()
    original = FakeView(window, None, BODY)
    original.type_at_row(3, "u")
    FakeView(other, None, buffer=original.buf)
    original.close()
    history = _history(window)
    assert history.entries == []


def test_window_close_forgets_history_and_erases_regions(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(10, "x")
    assert a.id() in edit_trail._State.trackable
    listener.on_pre_close_window(window)
    assert window.id() not in edit_trail._State.histories
    assert a.regions == {}
    assert a.id() not in edit_trail._State.trackable


def test_unload_erases_all_regions(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, BODY)
    a.type_at_row(10, "x")
    edit_trail.plugin_unloaded()
    assert a.regions == {}
    assert edit_trail._State.histories == {}
