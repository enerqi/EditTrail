"""Behaviour tests for edit_trail against the fake Sublime API in conftest.py."""

from __future__ import annotations

import os.path
from typing import TYPE_CHECKING

import pytest

import edit_trail
from tests.conftest import HUNDRED_LINE_TEXT, FakeView, FakeWindow, plugin_settings, run_timeouts, status_messages

if TYPE_CHECKING:
    from pathlib import Path

PATHS_ARE_CASE_INSENSITIVE = os.path.normcase("A") == "a"


def _history(window: FakeWindow) -> edit_trail.History:
    return edit_trail._State.histories[window.id()]


def _files(tmp_path: Path, *names: str) -> list[str]:
    paths: list[str] = []
    for name in names:
        path = tmp_path / name
        path.write_text(HUNDRED_LINE_TEXT)
        paths.append(str(path))
    return paths


def _locations(history: edit_trail.History) -> list[tuple[str | None, int]]:
    """(file name, current row) per entry, for readable assertions.

    An attached entry is reported from its live region; a detached one from the path and row it
    remembered.
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
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    a.type_at_row(12, "y")
    a.type_at_row(15, "z")
    assert _locations(_history(window)) == [(fa, 15)]


def test_far_edits_and_other_files_add_entries(window: FakeWindow, tmp_path: Path) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, HUNDRED_LINE_TEXT), FakeView(window, fb, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    a.type_at_row(50, "y")
    b.type_at_row(5, "z")
    assert _locations(_history(window)) == [(fa, 10), (fa, 50), (fb, 5)]


def test_edits_in_a_second_pane_merge_into_the_same_location(window: FakeWindow, tmp_path: Path) -> None:
    """A split pane shows the same buffer, so an edit in it is the same location, not a new one."""
    (fa,) = _files(tmp_path, "a.txt")
    pane_one = FakeView(window, fa, HUNDRED_LINE_TEXT)
    pane_two = FakeView(window, fa, buffer=pane_one.shared_buffer)
    pane_one.type_at_row(10, "x")
    pane_two.type_at_row(10, "y")
    pane_one.type_at_row(11, "z")
    assert _locations(_history(window)) == [(fa, 11)]


def test_a_merged_edit_moves_tracking_to_the_pane_it_was_typed_in(window: FakeWindow, tmp_path: Path) -> None:
    """Going back should land in the pane last edited, and leave no region behind in the other."""
    (fa,) = _files(tmp_path, "a.txt")
    pane_one = FakeView(window, fa, HUNDRED_LINE_TEXT)
    pane_two = FakeView(window, fa, buffer=pane_one.shared_buffer)
    pane_one.type_at_row(10, "x")
    pane_two.type_at_row(10, "y")

    entry = _history(window).entries[0]
    assert entry.live_view() is pane_two
    assert pane_one.get_regions(entry.key) == []


def test_far_edits_in_another_pane_still_add_an_entry(window: FakeWindow, tmp_path: Path) -> None:
    """Sharing a buffer is not enough: the edit still has to be near the newest location."""
    (fa,) = _files(tmp_path, "a.txt")
    pane_one = FakeView(window, fa, HUNDRED_LINE_TEXT)
    pane_two = FakeView(window, fa, buffer=pane_one.shared_buffer)
    pane_one.type_at_row(10, "x")
    pane_two.type_at_row(50, "y")
    assert _locations(_history(window)) == [(fa, 10), (fa, 50)]


def test_zero_merge_distance_only_merges_same_line(window: FakeWindow, tmp_path: Path) -> None:
    plugin_settings.set("merge_line_distance", 0)
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    a.type_at_row(10, "y")
    a.type_at_row(11, "z")
    assert _locations(_history(window)) == [(fa, 10), (fa, 11)]


def test_positions_follow_text_inserted_above(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(50, "x")
    a.type_at_row(0, "new\nnew\n")
    assert _locations(_history(window)) == [(fa, 52), (fa, 2)]


def test_scratch_and_panel_views_are_ignored(window: FakeWindow) -> None:
    FakeView(window, None, "", scratch=True).type(0, "terminal output")
    FakeView(window, None, "", element="output:exec").type(0, "build output")
    assert window.id() not in edit_trail._State.histories


def test_widget_views_are_ignored(window: FakeWindow) -> None:
    FakeView(window, None, "", widget=True).type(0, "find what")
    assert window.id() not in edit_trail._State.histories


def test_panel_and_widget_views_are_not_cached(window: FakeWindow) -> None:
    """Sublime reports no reliable close for them, so caching a "no" would leak one id each."""
    panel = FakeView(window, None, "", element="find:input")
    widget = FakeView(window, None, "", widget=True)
    for _ in range(3):
        panel.type(0, "x")
        widget.type(0, "y")
    assert edit_trail._State.trackable_view_ids == set()

    editor = FakeView(window, None, HUNDRED_LINE_TEXT)
    editor.type_at_row(10, "z")
    assert edit_trail._State.trackable_view_ids == {editor.id()}


def test_a_view_turned_scratch_after_an_edit_stops_being_recorded(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    a.scratch = True  # e.g. a plugin takes the buffer over to render a preview
    a.type_at_row(50, "y")
    assert [row for _, row in _locations(_history(window))] == [10]


def test_files_loading_or_restored_with_the_project_are_not_edits(window: FakeWindow, tmp_path: Path) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    FakeView(window, fa).external_change(0, HUNDRED_LINE_TEXT, loading=True)
    FakeView(window, fb).external_change(0, HUNDRED_LINE_TEXT)
    assert window.id() not in edit_trail._State.histories


def test_reload_from_disk_after_edits_adds_no_entry(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    a.cursor = a.text_point(80, 0)
    a.external_change(0, "changed on disk\n")
    assert _locations(_history(window)) == [(fa, 11)]


def test_changes_in_background_views_are_not_edits(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, HUNDRED_LINE_TEXT), FakeView(window, fb, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    b.dirty = True  # e.g. whitespace trimmed by save-all while a stays focused
    listener.on_modified(b)
    assert _locations(_history(window)) == [(fa, 10)]


def test_debug_setting_prints_decisions(window: FakeWindow, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plugin_settings.set("debug", True)
    (fa,) = _files(tmp_path, "a.txt")
    view = FakeView(window, fa)
    view.external_change(0, HUNDRED_LINE_TEXT)
    view.type_at_row(3, "x")
    view.type_at_row(4, "y")
    out = capsys.readouterr().out
    assert "ignored change with no unsaved edits" in out
    assert "recorded new location at line 4" in out
    # The second edit is one line away, so it merges: no new location, and the
    # message says so rather than claiming another one was recorded.
    assert "merged edit at line 5 into the newest location" in out
    assert len(_history(window).entries) == 1


def test_modification_with_no_selection_is_ignored(
    window: FakeWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    view = FakeView(window, fa, HUNDRED_LINE_TEXT)
    monkeypatch.setattr(view, "sel", list)  # an empty Selection raises IndexError when indexed
    view.type_at_row(3, "x")
    assert window.id() not in edit_trail._State.histories

    edit_trail._navigate(window, -1)  # no cursor to compare against either
    assert status_messages[-1] == "EditTrail: no edit locations recorded"


def test_max_entries_caps_history_and_erases_dropped_regions(window: FakeWindow, tmp_path: Path) -> None:
    plugin_settings.set("max_entries", 3)
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    for row in range(0, 100, 10):
        a.type_at_row(row, "e")
    assert [row for _, row in _locations(_history(window))] == [70, 80, 90]
    assert len(a.regions) == 3


def test_lowering_max_entries_trims_the_existing_history(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    for row in range(0, 100, 10):
        a.type_at_row(row, "e")
    history = _history(window)
    assert len(history.entries) == 10

    plugin_settings.set("max_entries", 2)
    assert [row for _, row in _locations(history)] == [80, 90]
    assert len(a.regions) == 2
    assert history.index == 2


def test_trimming_the_entry_you_are_on_does_not_strand_its_replacement(window: FakeWindow, tmp_path: Path) -> None:
    """A lowered max_entries that drops the entry the index points at returns to the head.

    Clamping the index to 0 instead would leave it on an entry never visited, which back cannot
    reach (is_enabled needs index > 0) and forward steps over.
    """
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    for row in (0, 20, 40, 60, 80):
        a.type_at_row(row, "e")
    history = _history(window)
    edit_trail._navigate(window, -1)
    edit_trail._navigate(window, -1)
    assert history.index == 2  # on the row 40 entry

    plugin_settings.set("max_entries", 2)  # drops rows 0, 20 and 40
    assert [row for _, row in _locations(history)] == [60, 80]
    assert history.index == 2  # the head, not clamped to 0

    edit_trail._navigate(window, -1)
    assert a.cursor_row() == 80
    edit_trail._navigate(window, -1)
    assert a.cursor_row() == 60  # reachable, not stranded


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
    a, b = FakeView(window, fa, HUNDRED_LINE_TEXT), FakeView(window, fb, HUNDRED_LINE_TEXT)
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
    a, b = FakeView(window, fa, HUNDRED_LINE_TEXT), FakeView(window, fb, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    b.type_at_row(20, "y")
    window.active = a
    edit_trail._navigate(window, -1)
    assert window.active is b


def test_back_skips_an_entry_showing_at_the_cursor_in_another_pane(window: FakeWindow, tmp_path: Path) -> None:
    """The other pane shows the same text, so going there would not move the user anywhere."""
    (fa,) = _files(tmp_path, "a.txt")
    pane_one = FakeView(window, fa, HUNDRED_LINE_TEXT)
    pane_one.type_at_row(10, "x")
    pane_one.type_at_row(50, "y")

    pane_two = FakeView(window, fa, buffer=pane_one.shared_buffer)
    pane_two.cursor = pane_two.text_point(50, 0)
    window.active = pane_two
    edit_trail._navigate(window, -1)

    assert window.active is pane_one
    assert pane_one.cursor_row() == 10


def test_new_edit_returns_to_head_without_truncating(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
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
    a, b = FakeView(window, fa, HUNDRED_LINE_TEXT), FakeView(window, fb, HUNDRED_LINE_TEXT)
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
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
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
    a, b = FakeView(window, fa, HUNDRED_LINE_TEXT), FakeView(window, fb, HUNDRED_LINE_TEXT)
    b.type_at_row(40, "k")
    a.type_at_row(5, "x")
    b.close()
    entry = _history(window).entries[0]
    assert entry.view is None
    assert (entry.file_name, entry.row) == (fb, 40)

    window.active = a
    edit_trail._navigate(window, -1)
    assert window.reopen_requests == [f"{fb}:41:2"]  # 1-based row, cursor after the typed "k"

    reopened = FakeView(window, fb, b.shared_buffer.text)
    listener.on_load(reopened)
    assert entry.live_view() is reopened
    assert _locations(_history(window))[0] == (fb, 40)


def test_reattach_clamps_to_a_file_that_shrank(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(90, "x")
    a.close()
    shorter = FakeView(window, fa, "only\ntwo lines")
    listener.on_load(shorter)
    point = _history(window).entries[0].point()
    assert point is not None
    assert 0 <= point <= shorter.size()


def test_revert_reanchors_entries_whose_region_was_dropped(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(90, "x")
    entry = _history(window).entries[0]
    a.revert("only\ntwo lines", keep_regions=False)
    point = entry.point()
    assert point is not None
    assert 0 <= point <= a.size()


def test_revert_reanchors_entries_on_a_clone_of_the_reverted_buffer(window: FakeWindow, tmp_path: Path) -> None:
    """Clones share the refilled buffer, so their regions dangle too, whichever view Sublime reports."""
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    clone = FakeView(window, fa, buffer=a.shared_buffer)
    clone.type_at_row(90, "x")
    entry = _history(window).entries[0]
    assert entry.view is clone

    a.revert("only\ntwo lines")  # on_revert fires for a, not the clone
    point = entry.point()
    assert point is not None
    assert 0 <= point <= clone.size()  # not left dangling past the new end of file


def test_reload_refreshes_the_fallback_row_from_a_surviving_region(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(50, "x")
    a.type_at_row(0, "new\nnew\n")  # the first entry's region slides down to row 52
    entry = _history(window).entries[0]
    assert entry.row == 50  # the idle refresh has not run yet

    a.reload_from_disk(a.shared_buffer.text)
    assert entry.row == 52


def test_idle_refresh_moves_the_fallback_row_with_the_region(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(50, "x")
    a.type_at_row(0, "new\n" * 100)  # far away: its own entry, and the first one slides to row 150
    entry = _history(window).entries[0]
    assert entry.row == 50

    run_timeouts()  # the editor goes idle
    assert entry.row == 150


def test_idle_refresh_follows_a_clone_of_the_modified_buffer(window: FakeWindow) -> None:
    """Typing in one view shifts the regions of every view of its buffer, so clones refresh too."""
    a = FakeView(window, None, HUNDRED_LINE_TEXT)
    clone = FakeView(window, None, buffer=a.shared_buffer)
    a.type_at_row(50, "x")
    entry = _history(window).entries[0]
    entry.move_to_clone(a, clone)  # region in the clone now, row/col left as it was

    a.type_at_row(0, "new\n" * 100)
    run_timeouts()

    assert entry.row == 150


def test_idle_refresh_leaves_entries_on_untouched_buffers_alone(window: FakeWindow, tmp_path: Path) -> None:
    """Only buffers modified since the last run can have shifted, so only those cost round trips."""
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, HUNDRED_LINE_TEXT), FakeView(window, fb, HUNDRED_LINE_TEXT)
    a.type_at_row(50, "x")
    b.type_at_row(50, "y")
    run_timeouts()
    entry_a, entry_b = _history(window).entries

    # Shift a's region without going through on_modified, so a refresh that swept
    # everything would pick the change up and a correctly scoped one would too.
    a._insert(0, "new\n" * 100)
    # Shift b's the same way, but keep b out of the modified set.
    b._insert(0, "new\n" * 100)
    a.type_at_row(0, "z")  # only a is reported modified
    run_timeouts()

    assert entry_a.row == 150
    assert entry_b.row == 50  # not visited: b's buffer was never reported modified


def test_idle_refresh_sweeps_everything_when_a_modified_view_has_closed(window: FakeWindow, tmp_path: Path) -> None:
    """A dead view cannot name its buffer, so the run falls back to refreshing every entry."""
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, HUNDRED_LINE_TEXT), FakeView(window, fb, HUNDRED_LINE_TEXT)
    a.type_at_row(50, "x")
    b.type_at_row(50, "y")
    run_timeouts()
    entry_b = _history(window).entries[1]

    b._insert(0, "new\n" * 100)
    a.type_at_row(0, "z")
    a.close()  # modified inside the throttle window, then gone before it fired
    run_timeouts()

    assert entry_b.row == 150


def test_a_dropped_region_reanchors_where_the_edit_ended_up(window: FakeWindow, tmp_path: Path) -> None:
    """Without the idle refresh this lands on row 50, where the entry was first typed."""
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(50, "x")
    a.type_at_row(0, "new\n" * 100)
    entry = _history(window).entries[0]
    run_timeouts()

    a.reload_from_disk(a.shared_buffer.text, keep_regions=False)
    point = entry.point()
    assert point is not None
    assert a.rowcol(point)[0] == 150


@pytest.mark.skipif(not PATHS_ARE_CASE_INSENSITIVE, reason="paths are case-sensitive on this platform")
def test_reopening_with_a_differently_cased_path_reattaches(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    a.close()

    reopened = FakeView(window, fa.upper(), HUNDRED_LINE_TEXT)
    listener.on_load(reopened)
    assert _history(window).entries[0].live_view() is reopened


def test_entries_for_deleted_files_are_dropped_and_skipped(window: FakeWindow, tmp_path: Path) -> None:
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, HUNDRED_LINE_TEXT), FakeView(window, fb, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    b.type_at_row(20, "y")
    b.close()
    (tmp_path / "b.txt").unlink()

    window.active = None
    edit_trail._navigate(window, -1)
    assert window.active is a
    assert window.reopen_requests == []
    assert [name for name, _ in _locations(_history(window))] == [fa]


def test_unsaved_buffer_entries_move_to_clone_then_drop(window: FakeWindow) -> None:
    original = FakeView(window, None, HUNDRED_LINE_TEXT)
    original.type_at_row(3, "u")
    clone = FakeView(window, None, buffer=original.shared_buffer)
    original.close()
    history = _history(window)
    assert history.entries[0].live_view() is clone
    assert clone.get_regions(history.entries[0].key)

    clone.close()
    assert history.entries == []


def test_entry_is_reopenable_when_its_view_dies_without_on_pre_close(window: FakeWindow, tmp_path: Path) -> None:
    """Sublime does not reliably send on_pre_close when a whole window goes."""
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    entry = _history(window).entries[0]

    window._views.remove(a)  # the tab was dragged to another window, which then closed
    a.valid = False
    assert entry.live_view() is None
    assert entry.is_reachable()

    FakeView(window, None, HUNDRED_LINE_TEXT).type_at_row(0, "elsewhere")
    edit_trail._navigate(window, -1)
    assert window.reopen_requests == [f"{fa}:11:2"]


def test_reopening_reattaches_when_the_view_died_without_on_pre_close(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    """The entry holds a dead view, not None, so re-attaching has to test liveness."""
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    entry = _history(window).entries[0]
    window._views.remove(a)  # the tab was dragged to another window, which then closed
    a.valid = False

    reopened = FakeView(window, fa, HUNDRED_LINE_TEXT)
    listener.on_load(reopened)
    assert entry.live_view() is reopened
    assert reopened.get_regions(entry.key)

    reopened.type_at_row(11, "y")  # tracking again, so a nearby edit merges rather than piling up
    assert _locations(_history(window)) == [(fa, 11)]


def test_reopening_leaves_an_entry_already_tracking_in_a_live_view_alone(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    """Opening a second view of an open file must not move entries off the view they are in."""
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    entry = _history(window).entries[0]

    second = FakeView(window, fa, buffer=a.shared_buffer)
    listener.on_load(second)
    assert entry.live_view() is a


def test_buffer_saved_after_edit_is_reopenable(window: FakeWindow, tmp_path: Path) -> None:
    view = FakeView(window, None, HUNDRED_LINE_TEXT)
    view.type_at_row(7, "x")
    (path,) = _files(tmp_path, "saved.txt")
    view.path = path  # simulates Save As
    view.close()
    entry = _history(window).entries[0]
    assert entry.file_name == path
    assert entry.is_reachable()


# Window isolation


def _drag_tab_to(view: FakeView, window: FakeWindow) -> None:
    """Simulate dragging a tab into another window."""
    view.owning_window._views.remove(view)
    view.owning_window = window
    window._views.append(view)


def test_navigation_never_follows_a_tab_dragged_to_another_window(window: FakeWindow, tmp_path: Path) -> None:
    other = FakeWindow()
    fa, fb = _files(tmp_path, "a.txt", "b.txt")
    a, b = FakeView(window, fa, HUNDRED_LINE_TEXT), FakeView(window, fb, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    b.type_at_row(20, "y")
    _drag_tab_to(b, other)
    window.active = a

    edit_trail._navigate(window, -1)
    assert other.active is None
    assert window.active is a
    assert a.cursor_row() == 10
    assert [name for name, _ in _locations(_history(window))] == [fa]


def test_reopening_a_file_in_another_window_does_not_take_its_entries(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    other = FakeWindow()
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    a.close()

    elsewhere = FakeView(other, fa, HUNDRED_LINE_TEXT)
    listener.on_load(elsewhere)
    assert _history(window).entries[0].view is None

    here = FakeView(window, fa, HUNDRED_LINE_TEXT)
    listener.on_load(here)
    assert _history(window).entries[0].live_view() is here


def test_unsaved_buffer_entries_do_not_move_to_a_clone_in_another_window(window: FakeWindow) -> None:
    other = FakeWindow()
    original = FakeView(window, None, HUNDRED_LINE_TEXT)
    original.type_at_row(3, "u")
    FakeView(other, None, buffer=original.shared_buffer)
    original.close()
    history = _history(window)
    assert history.entries == []


def test_entries_are_not_handed_a_clone_in_the_window_the_tab_moved_to(window: FakeWindow, tmp_path: Path) -> None:
    other = FakeWindow()
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    entry = _history(window).entries[0]

    window._views.remove(a)  # the tab is dragged to the other window
    a.owning_window = other
    other._views.append(a)
    clone = FakeView(other, fa, buffer=a.shared_buffer)
    a.close()

    assert entry.view is None  # detached, not moved to a clone this window cannot focus
    assert entry.file_name == fa
    assert clone.get_regions(entry.key) == []


def test_window_close_forgets_history_and_erases_regions(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    assert a.id() in edit_trail._State.trackable_view_ids
    listener.on_pre_close_window(window)
    assert window.id() not in edit_trail._State.histories
    assert a.regions == {}
    assert a.id() not in edit_trail._State.trackable_view_ids


def test_unload_erases_all_regions(window: FakeWindow, tmp_path: Path) -> None:
    (fa,) = _files(tmp_path, "a.txt")
    a = FakeView(window, fa, HUNDRED_LINE_TEXT)
    a.type_at_row(10, "x")
    edit_trail.plugin_unloaded()
    assert a.regions == {}
    assert edit_trail._State.histories == {}


def test_closing_the_view_of_the_entry_you_are_on_leaves_no_entry_stranded(window: FakeWindow) -> None:
    """Dropping the entry ``index`` points at returns to the head, keeping every entry reachable.

    The dropped entry is an unsaved buffer's only view, so on_pre_close has nothing to reopen and
    nowhere to move it. Leaving ``index`` on the slot would make both directions step over the entry
    that slid into it.
    """
    views = [FakeView(window, None if i == 1 else f"/f{i}.txt", HUNDRED_LINE_TEXT) for i in range(4)]
    for i, view in enumerate(views):
        view.type_at_row(i * 20, "x")
    history = _history(window)
    back = edit_trail.EditTrailBackCommand(window)
    back.run()
    back.run()
    assert history.index == 1  # standing on the unsaved buffer's entry
    unsaved = history.entries[1]

    views[1].close()

    assert unsaved not in history.entries
    assert history.index == len(history.entries)  # back at the head
    # The entry that took slot 1 is still reachable, and nothing is skipped.
    assert [row for _, row in _locations(history)] == [0, 40, 60]
    back.run()
    back.run()
    back.run()
    assert [entry.row for entry in history.entries][history.index] == 0


def test_window_close_forgets_the_cached_kind_of_a_preview_tab(
    window: FakeWindow, listener: edit_trail.EditTrailListener, tmp_path: Path
) -> None:
    """Window.views() omits the preview tab, so the sweep has to ask for it explicitly."""
    (fa,) = _files(tmp_path, "a.txt")
    preview = FakeView(window, fa, HUNDRED_LINE_TEXT, transient=True)
    preview.type_at_row(10, "x")
    assert preview.id() in edit_trail._State.trackable_view_ids
    listener.on_pre_close_window(window)
    assert preview.id() not in edit_trail._State.trackable_view_ids
