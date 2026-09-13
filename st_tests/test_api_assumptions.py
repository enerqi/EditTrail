"""Check, inside the editor, the Sublime API behaviour edit_trail relies on.

The unit tests in ``tests/`` drive edit_trail against a fake Sublime API (``tests/conftest.py``).
That fake encodes what this plugin believes the editor does: that a region shifts as text is inserted
before it, that ``get_regions`` hands back copies, that views of one buffer share its text but not
its regions, that a revert may drop a region or leave it past the end of a file that shrank. None of
that is in the API's type annotations, most of it is not in its documentation either, and nothing in
the unit tests would notice if a Sublime release changed it: the fake would keep answering the old
way.

These run against the real editor instead, under UnitTesting_, which drives them inside Sublime and
in CI against whichever builds the workflow asks for. A failure means the plugin is wrong about this
build, and the matching line in edit_trail's "API contract relied on" section needs revisiting.

Run them from the command palette with ``UnitTesting: Test Current Package`` after linking the
checkout in (``just install-dev``), or let ``.github/workflows/tests.yml`` run them.

They work in a scratch view and a temporary file, and close what they open. They touch none of your
files and none of the plugin's state, so a run is safe while using EditTrail normally: scratch views
are ignored by the plugin itself, and the temporary file is never typed in, so a run leaves no edit
locations behind.

Two behaviours are recorded rather than required, because ``Entry.reanchor`` copes with either: what
a revert does to a region left past the new end of file, and what an empty region list means. Those
tests assert the set of outcomes the plugin handles and print which one this build chose.

.. _UnitTesting: https://github.com/SublimeText/UnitTesting
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sublime
from unittesting import DeferrableTestCase

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

TRACKING_REGION_KEY = "edit_trail_selftest"
"""Region key for the checks, namespaced away from the plugin's own keys."""

SCRATCH_TEXT = "0123456789"
FILE_TEXT = "alpha\nbravo\ncharlie\ndelta\necho\n"
SHORTER_FILE_TEXT = "alpha\n"
"""What the temporary file becomes on disk before the revert, so the region ends up past its end."""


def _place(view: sublime.View, point: int) -> None:
    """Place a hidden, zero-width tracking region, exactly as Entry._place does."""
    view.add_regions(TRACKING_REGION_KEY, [sublime.Region(point)], flags=sublime.HIDDEN)


def _region_point(view: sublime.View) -> int | None:
    """Where the tracking region is in ``view``, or None if it has none."""
    regions = view.get_regions(TRACKING_REGION_KEY)
    return regions[0].b if regions else None


def _edit(
    view: sublime.View,
    operation: str,
    point: int = 0,
    text: str = "",
    erase_from: int = 0,
    erase_to: int = 0,
) -> None:
    """Run a text change through the helper TextCommand in edit_trail_selftest.py.

    A TextCommand is the only way to get an Edit token, so the change cannot be made from here.
    """
    args: dict[str, Any] = {
        "operation": operation,
        "point": point,
        "text": text,
        "erase_from": erase_from,
        "erase_to": erase_to,
    }
    view.run_command("edit_trail_selftest_edit", args)


def _close(view: sublime.View | None) -> None:
    """Close a view the tests opened, without a save prompt."""
    if view is None or not view.is_valid():
        return
    view.set_scratch(True)
    view.close()


def _note(message: str) -> None:
    """Record a behaviour the plugin copes with either way, so a change to it is visible in the log."""
    print("OBSERVED: " + message)  # noqa: T201 - the test log is where this belongs


class _ScratchViewTest(DeferrableTestCase):
    """Base for the checks that need one scratch view with known contents."""

    def setUp(self) -> None:
        self.view = sublime.active_window().new_file()
        self.view.set_scratch(True)
        self.view.set_name("EditTrail API assumptions")
        _edit(self.view, operation="replace_all", text=SCRATCH_TEXT)

    def tearDown(self) -> None:
        _close(self.view)


class TestTrackingRegions(_ScratchViewTest):
    """How regions move with the text.

    ``Entry.merge``, ``Entry.point`` and ``_refresh_anchors`` all rest on this: the plugin rewrites a
    region only when Sublime has not already moved it.
    """

    def test_a_hidden_zero_width_region_lands_where_it_was_placed(self) -> None:
        _place(self.view, 5)
        regions = self.view.get_regions(TRACKING_REGION_KEY)
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].b, 5)
        self.assertEqual(regions[0].a, regions[0].b, "the region did not stay zero-width")

    def test_get_regions_returns_copies_not_live_handles(self) -> None:
        _place(self.view, 5)
        regions = self.view.get_regions(TRACKING_REGION_KEY)
        regions[0].a = regions[0].b = 0
        self.assertEqual(_region_point(self.view), 5)

    def test_a_region_shifts_when_text_is_inserted_before_it(self) -> None:
        _place(self.view, 5)
        _edit(self.view, operation="insert", point=0, text="ab")
        self.assertEqual(_region_point(self.view), 7)

    def test_a_region_is_unmoved_by_text_inserted_after_it(self) -> None:
        _place(self.view, 5)
        _edit(self.view, operation="insert", point=self.view.size(), text="cd")
        self.assertEqual(_region_point(self.view), 5)

    def test_a_region_shifts_back_when_text_before_it_is_deleted(self) -> None:
        _place(self.view, 5)
        _edit(self.view, operation="erase", erase_from=0, erase_to=2)
        self.assertEqual(_region_point(self.view), 3)

    def test_re_adding_a_key_replaces_its_regions(self) -> None:
        _place(self.view, 5)
        _place(self.view, 1)
        self.assertEqual([r.b for r in self.view.get_regions(TRACKING_REGION_KEY)], [1])

    def test_erase_regions_removes_the_key(self) -> None:
        _place(self.view, 5)
        self.view.erase_regions(TRACKING_REGION_KEY)
        self.assertEqual(self.view.get_regions(TRACKING_REGION_KEY), [])

    def test_add_regions_with_an_empty_list_leaves_the_key_with_no_regions(self) -> None:
        """Recorded, not required: ``Entry.point`` treats "no regions" the same however it arose."""
        _place(self.view, 5)
        self.view.add_regions(TRACKING_REGION_KEY, [], flags=sublime.HIDDEN)
        regions = self.view.get_regions(TRACKING_REGION_KEY)
        _note(f"add_regions with an empty list leaves get_regions -> {regions!r}")
        self.assertEqual(regions, [])

    def test_a_region_whose_text_is_all_deleted_stays_inside_the_buffer(self) -> None:
        """Recorded, not required: dropped or clamped to 0, both are positions ``Entry.point`` can use."""
        _place(self.view, self.view.size() - 1)
        _edit(self.view, operation="erase", erase_from=0, erase_to=self.view.size())
        point = _region_point(self.view)
        _note(f"a region whose text is all deleted ends at {point!r} in an empty buffer")
        self.assertIn(point, (None, 0))


class TestSelection(_ScratchViewTest):
    """The selection.

    ``on_modified`` reads ``view.sel()[0]`` and relies on an empty selection raising IndexError
    rather than returning something; ``_go`` clears and re-adds through a handle it took earlier.
    """

    def test_sel_is_backed_by_the_view_so_clear_empties_it(self) -> None:
        self.view.sel().clear()
        self.assertEqual(len(self.view.sel()), 0)

    def test_indexing_an_empty_selection_raises_index_error(self) -> None:
        self.view.sel().clear()
        with self.assertRaises(IndexError):
            self.view.sel()[0]

    def test_a_selection_taken_earlier_still_mutates_the_view(self) -> None:
        selection = self.view.sel()
        selection.clear()
        selection.add(sublime.Region(2))
        self.assertEqual(len(self.view.sel()), 1)
        self.assertEqual(self.view.sel()[0].b, 2)


class TestClones(_ScratchViewTest):
    """Clones.

    ``Entry.move_to_clone`` and ``_refresh_anchors`` assume views of one buffer share its text (so an
    edit in either moves both views' regions) but not its regions.
    """

    def setUp(self) -> None:
        super().setUp()
        self.clone: sublime.View | None = None

    def tearDown(self) -> None:
        _close(self.clone)
        super().tearDown()

    def _open_clone(self) -> Generator[Any, None, sublime.View]:
        """Clone the scratch view and hand back the second view of its buffer.

        Cloning is asynchronous, so the test waits for the view to appear rather than assuming it is
        there on the next line.
        """
        window = self.view.window()
        self.assertIsNotNone(window, "the scratch view is not in a window")
        assert window is not None
        window.focus_view(self.view)
        window.run_command("clone_file")
        yield lambda: self._find_clone(window) is not None
        clone = self._find_clone(window)
        self.assertIsNotNone(clone, "clone_file produced no second view of the buffer")
        assert clone is not None
        self.clone = clone
        return clone

    def _find_clone(self, window: sublime.Window) -> sublime.View | None:
        buffer_id = self.view.buffer_id()
        return next(
            (other for other in window.views() if other.id() != self.view.id() and other.buffer_id() == buffer_id),
            None,
        )

    def test_a_clone_shares_the_buffer_id_but_not_the_regions(self) -> Iterator[Any]:
        _place(self.view, 5)
        clone = yield from self._open_clone()
        self.assertEqual(clone.buffer_id(), self.view.buffer_id())
        self.assertEqual(clone.buffer().id(), clone.buffer_id(), "buffer() disagrees with buffer_id()")
        self.assertEqual(
            clone.get_regions(TRACKING_REGION_KEY), [], "regions turned out to be per buffer, not per view"
        )

    def test_an_edit_in_a_clone_shifts_the_original_views_region(self) -> Iterator[Any]:
        _place(self.view, 5)
        clone = yield from self._open_clone()
        _edit(clone, operation="insert", point=0, text="xy")
        self.assertEqual(_region_point(self.view), 7)


class TestBufferRefilledFromDisk(DeferrableTestCase):
    """A buffer refilled from disk.

    ``Entry.reanchor`` exists because Sublime may drop a tracking region here, or leave it past the
    end of a file that shrank. Either is fine, and which one happens is what these record.
    """

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="edit_trail_api_"))
        self.path = self.temp_dir / "assumptions.txt"
        self.path.write_text(FILE_TEXT, encoding="utf-8")
        self.view: sublime.View | None = None

    def tearDown(self) -> None:
        _close(self.view)
        shutil.rmtree(str(self.temp_dir), ignore_errors=True)

    def _open_file(self) -> Generator[Any, None, sublime.View]:
        """Open the temporary file and wait for Sublime to finish filling it."""
        self.view = sublime.active_window().open_file(str(self.path))
        view = self.view
        yield lambda: view.is_valid() and not view.is_loading()
        return view

    def _revert_to_shorter_file(self, view: sublime.View) -> Iterator[Any]:
        """Shrink the file on disk and revert, so the region's old position is past the new end."""
        self.path.write_text(SHORTER_FILE_TEXT, encoding="utf-8")
        view.run_command("revert")
        yield lambda: view.is_valid() and view.size() == len(SHORTER_FILE_TEXT)

    def test_a_file_opens_with_the_content_on_disk(self) -> Iterator[Any]:
        view = yield from self._open_file()
        self.assertEqual(view.size(), len(FILE_TEXT))

    def test_a_different_buffer_has_a_different_id(self) -> Iterator[Any]:
        view = yield from self._open_file()
        other = sublime.active_window().new_file()
        try:
            self.assertNotEqual(view.buffer_id(), other.buffer_id())
        finally:
            _close(other)

    def test_a_revert_leaves_the_region_droppable_or_usable(self) -> Iterator[Any]:
        """Recorded, not required: ``Entry.reanchor`` handles dropped, dangling and clamped alike."""
        view = yield from self._open_file()
        _place(view, view.text_point(2, 0))
        yield from self._revert_to_shorter_file(view)

        after = _region_point(view)
        if after is None:
            _note("a revert that shrank the file dropped the tracking region")
        elif after > view.size():
            _note(f"a revert that shrank the file left the region past the end ({after} > {view.size()})")
        else:
            _note(f"a revert that shrank the file kept the region, clamped to {after}")
        self.assertIn(len(view.get_regions(TRACKING_REGION_KEY)), (0, 1), "a revert multiplied the tracking region")

    def test_a_revert_leaves_the_buffer_clean_so_on_modified_can_reject_it(self) -> Iterator[Any]:
        view = yield from self._open_file()
        yield from self._revert_to_shorter_file(view)
        self.assertFalse(view.is_dirty())
