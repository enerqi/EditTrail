"""Dev-only: check, inside the editor, the Sublime API behaviour edit_trail relies on.

The unit tests drive edit_trail against a fake Sublime API (``tests/conftest.py``). That fake
encodes what this plugin believes the editor does: that a region shifts as text is inserted before
it, that ``get_regions`` hands back copies, that views of one buffer share its text but not its
regions, that a revert may drop a region or leave it past the end of a file that shrank. None of
that is in the API's type annotations, most of it is not in its documentation either, and nothing
in the test suite would notice if a Sublime release changed it: the fake would keep answering the
old way.

This command runs those assumptions against the real editor. Run it after a Sublime upgrade, from
the console (View > Show Console), where ``window`` is already in scope::

    window.run_command("edit_trail_selftest")

It works in a scratch view and a temporary file, reports to the console and an output panel, and
closes what it opened. It touches none of your files and none of the plugin's state, so it is safe
to run while using EditTrail normally. Scratch views are ignored by the plugin itself, and the
temporary file is never typed in, so a run leaves no edit locations behind.

Deliberately not in the command palette: it is a development tool, and ``.gitattributes`` keeps
this file out of the archive Package Control installs from, so users never receive it.

Results
-------
``PASS`` / ``FAIL``
    An assumption edit_trail's correctness depends on. A FAIL means the plugin is wrong about this
    build, and the matching line in edit_trail's "API contract relied on" section needs revisiting.
``OBSERVED``
    Behaviour the plugin copes with either way, recorded so that a change to it is visible. The
    interesting one is what a revert does to a tracking region: whichever way it goes,
    ``Entry.reanchor`` handles it, and this says which way it went.
``ERROR``
    A check could not be run (a view would not open, a revert never completed). Counted as a
    failure, because an unrun check is not a passing one.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import sublime
import sublime_plugin

if TYPE_CHECKING:
    from collections.abc import Callable

    Step = Callable[[], None]

KEY = "edit_trail_selftest"
"""Region key for the checks, namespaced away from the plugin's own keys."""

POLL_MS = 50
POLL_TRIES = 100
"""50ms x 100: five seconds, enough for a file to open or revert on a slow disk."""

SCRATCH_TEXT = "0123456789"
FILE_TEXT = "alpha\nbravo\ncharlie\ndelta\necho\n"
SHORTER_FILE_TEXT = "alpha\n"
"""What the temporary file becomes on disk before the revert, so the region ends up past its end."""


class _Report:
    """Check results, printed once the last stage has run."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failed = 0

    def check(self, label: str, passed: bool) -> None:
        self.lines.append(("PASS     " if passed else "FAIL     ") + label)
        if not passed:
            self.failed += 1

    def observe(self, label: str) -> None:
        self.lines.append("OBSERVED " + label)

    def error(self, label: str) -> None:
        self.lines.append("ERROR    " + label)
        self.failed += 1

    def publish(self, window: sublime.Window) -> None:
        """Print the report to the console and show it in an output panel."""
        summary = (
            "all assumptions hold"
            if not self.failed
            else f"{self.failed} failed - edit_trail's API assumptions no longer match this build"
        )
        text = f"EditTrail selftest ({sublime.version()}): {summary}\n\n" + "\n".join(self.lines) + "\n"
        print(text)  # noqa: T201 - the console is where a dev command reports
        panel = window.create_output_panel(KEY)
        panel.run_command("append", {"characters": text, "scroll_to_end": True})
        window.run_command("show_panel", {"panel": "output." + KEY})
        sublime.status_message("EditTrail selftest: " + summary)


def _region_point(view: sublime.View) -> int | None:
    """Where the tracking region is in ``view``, or None if it has none."""
    regions = view.get_regions(KEY)
    return regions[0].b if regions else None


def _place(view: sublime.View, point: int) -> None:
    """Place a hidden, zero-width tracking region, exactly as Entry._place does."""
    view.add_regions(KEY, [sublime.Region(point)], flags=sublime.HIDDEN)


def _edit(view: sublime.View, op: str, point: int = 0, text: str = "", a: int = 0, b: int = 0) -> None:
    """Run a text change through the helper TextCommand below."""
    args: dict[str, sublime.Value] = {"op": op, "point": point, "text": text, "a": a, "b": b}
    view.run_command("edit_trail_selftest_edit", args)


def _close(view: sublime.View | None) -> None:
    """Close a view the selftest opened, without a save prompt."""
    if view is None or not view.is_valid():
        return
    view.set_scratch(True)
    view.close()


class _Selftest:
    """Runs the checks in stages, because opening, cloning and reverting are all asynchronous."""

    def __init__(self, window: sublime.Window, report: _Report) -> None:
        self.window = window
        self.report = report
        self.scratch: sublime.View | None = None
        self.clone: sublime.View | None = None
        self.file_view: sublime.View | None = None
        self.temp_dir: Path | None = None

    # Stage plumbing

    def run(self) -> None:
        self.stage_regions()

    def poll(self, ready: Callable[[], bool], then: Step, label: str, tries: int = POLL_TRIES) -> None:
        """Call ``then`` once ``ready`` is true, giving up (and reporting) after POLL_TRIES."""
        if ready():
            then()
            return
        if tries <= 0:
            self.report.error(f"{label}: timed out after {POLL_MS * POLL_TRIES}ms")
            self.finish()
            return
        sublime.set_timeout(lambda: self.poll(ready, then, label, tries - 1), POLL_MS)

    def finish(self) -> None:
        _close(self.clone)
        _close(self.scratch)
        _close(self.file_view)
        if self.temp_dir is not None:
            shutil.rmtree(str(self.temp_dir), ignore_errors=True)
        self.report.publish(self.window)

    # Stage 1: how regions move with the text. Entry.merge, Entry.point and _refresh_anchors all
    # rest on this: the plugin rewrites a region only when Sublime has not already moved it.

    def stage_regions(self) -> None:
        view = self.window.new_file()
        self.scratch = view
        view.set_scratch(True)
        view.set_name("EditTrail selftest")
        _edit(view, op="replace_all", text=SCRATCH_TEXT)

        _place(view, 5)
        self.report.check("a hidden zero-width region lands where it was placed", _region_point(view) == 5)

        regions = view.get_regions(KEY)
        self.report.check("it stays zero-width", bool(regions) and regions[0].a == regions[0].b)
        if regions:
            regions[0].a = regions[0].b = 0
        self.report.check("get_regions returns copies, not live handles", _region_point(view) == 5)

        _edit(view, op="insert", point=0, text="ab")
        self.report.check("a region shifts when text is inserted before it", _region_point(view) == 7)

        _edit(view, op="insert", point=view.size(), text="cd")
        self.report.check("a region is unmoved by text inserted after it", _region_point(view) == 7)

        _edit(view, op="erase", a=0, b=2)
        self.report.check("a region shifts back when text before it is deleted", _region_point(view) == 5)

        _place(view, 1)
        self.report.check("re-adding a key replaces its regions", [r.b for r in view.get_regions(KEY)] == [1])

        _place(view, 5)
        view.add_regions(KEY, [], flags=sublime.HIDDEN)
        self.report.observe(f"add_regions with an empty list leaves get_regions -> {view.get_regions(KEY)!r}")

        _place(view, view.size() - 1)
        _edit(view, op="erase", a=0, b=view.size())
        self.report.observe(f"a region whose text is all deleted ends at {_region_point(view)!r} in an empty buffer")

        _edit(view, op="replace_all", text=SCRATCH_TEXT)
        _place(view, 5)
        view.erase_regions(KEY)
        self.report.check("erase_regions removes the key", view.get_regions(KEY) == [])

        self.stage_selection()

    # Stage 2: the selection. on_modified reads view.sel()[0] and relies on an empty selection
    # raising IndexError rather than returning something; _go clears and re-adds through a handle.

    def stage_selection(self) -> None:
        view = self.scratch
        if view is None:
            return
        selection = view.sel()
        selection.clear()
        self.report.check("sel() is backed by the view: clear() empties it", len(view.sel()) == 0)

        raised = False
        try:
            view.sel()[0]
        except IndexError:
            raised = True
        self.report.check("indexing an empty selection raises IndexError", raised)

        selection.add(sublime.Region(2))
        self.report.check(
            "a Selection taken earlier still mutates the view",
            len(view.sel()) == 1 and view.sel()[0].b == 2,
        )

        self.stage_clone()

    # Stage 3: clones. Entry.move_to_clone and _refresh_anchors assume views of one buffer share
    # its text (so an edit in either moves both views' regions) but not its regions.

    def stage_clone(self) -> None:
        view = self.scratch
        if view is None:
            return
        _place(view, 5)
        self.window.focus_view(view)
        self.window.run_command("clone_file")
        sublime.set_timeout(self.check_clone, POLL_MS)

    def check_clone(self) -> None:
        view = self.scratch
        if view is None:
            return
        buffer_id = view.buffer_id()
        clone = next(
            (other for other in self.window.views() if other.id() != view.id() and other.buffer_id() == buffer_id),
            None,
        )
        if clone is None:
            self.report.error("clone_file produced no second view of the buffer")
            self.stage_revert()
            return
        self.clone = clone

        self.report.check("a clone shares the buffer id", clone.buffer_id() == view.buffer_id())
        self.report.check("buffer() agrees with buffer_id()", clone.buffer().id() == clone.buffer_id())
        self.report.check("regions are per view, not per buffer", clone.get_regions(KEY) == [])

        _edit(clone, op="insert", point=0, text="xy")
        self.report.check("an edit in a clone shifts the original view's region", _region_point(view) == 7)

        _close(clone)
        self.clone = None
        self.stage_revert()

    # Stage 4: a buffer refilled from disk. Entry.reanchor exists because Sublime may drop a
    # tracking region here, or leave it past the end of a file that shrank. Either is fine; which
    # one happens is what this records.

    def stage_revert(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="edit_trail_selftest_"))
        path = self.temp_dir / "selftest.txt"
        path.write_text(FILE_TEXT, encoding="utf-8")
        self.file_view = self.window.open_file(str(path))
        view = self.file_view
        self.poll(
            lambda: view.is_valid() and not view.is_loading(),
            self.check_revert,
            "opening the temporary file",
        )

    def check_revert(self) -> None:
        view = self.file_view
        path = None if self.temp_dir is None else self.temp_dir / "selftest.txt"
        if view is None or path is None:
            return
        self.report.check("a file opens with the content on disk", view.size() == len(FILE_TEXT))
        scratch = self.scratch
        self.report.check(
            "a different buffer has a different id",
            scratch is None or not scratch.is_valid() or view.buffer_id() != scratch.buffer_id(),
        )

        point = view.text_point(2, 0)
        _place(view, point)
        # Shrink the file on disk, then revert: the region's old position is now past the end.
        path.write_text(SHORTER_FILE_TEXT, encoding="utf-8")
        view.run_command("revert")
        self.poll(
            lambda: view.is_valid() and view.size() == len(SHORTER_FILE_TEXT),
            self.report_revert,
            "reverting the temporary file",
        )

    def report_revert(self) -> None:
        view = self.file_view
        if view is None:
            return
        after = _region_point(view)
        if after is None:
            self.report.observe("a revert that shrank the file dropped the tracking region")
        elif after > view.size():
            self.report.observe(f"a revert that shrank the file left the region past the end ({after} > {view.size()})")
        else:
            self.report.observe(f"a revert that shrank the file kept the region, clamped to {after}")
        self.report.check(
            "a revert leaves the buffer clean, so on_modified can reject it",
            not view.is_dirty(),
        )
        self.finish()


class EditTrailSelftestCommand(sublime_plugin.WindowCommand):
    """Check the Sublime API assumptions edit_trail is built on. See the module docstring."""

    def run(self, **_kwargs: object) -> None:
        """Run the checks. Takes no arguments; ``**_kwargs`` matches the base signature."""
        _Selftest(self.window, _Report()).run()


class EditTrailSelftestEditCommand(sublime_plugin.TextCommand):
    """Apply one text change for the selftest: a TextCommand is the only way to get an Edit token."""

    def run(self, edit: sublime.Edit, **kwargs: sublime.Value) -> None:
        """Insert ``text`` at ``point``, erase ``a``-``b``, or replace the whole buffer with ``text``.

        Arguments arrive as a dict because Sublime passes command arguments as keywords typed
        ``Value``; they are narrowed here rather than annotated in the signature, which must stay
        compatible with ``TextCommand.run``.
        """
        op = str(kwargs.get("op", ""))
        text = str(kwargs.get("text", ""))
        point = int(kwargs.get("point", 0))  # ty: ignore[invalid-argument-type]
        a = int(kwargs.get("a", 0))  # ty: ignore[invalid-argument-type]
        b = int(kwargs.get("b", 0))  # ty: ignore[invalid-argument-type]
        if op == "insert":
            self.view.insert(edit, point, text)
        elif op == "erase":
            self.view.erase(edit, sublime.Region(a, b))
        elif op == "replace_all":
            self.view.replace(edit, sublime.Region(0, self.view.size()), text)
        else:
            message = f"unknown selftest edit op: {op}"
            raise ValueError(message)
