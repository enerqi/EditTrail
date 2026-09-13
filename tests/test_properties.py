"""Stateful property tests: random command sequences against the fake Sublime API.

The tests in ``test_edit_trail.py`` are examples. Each one names a situation and asserts what should
happen in it, which is what documents the behaviour, but each one also fixes the order its commands
run in. The bugs this plugin has actually had were ordering bugs: an index left pointing at a slot a
dropped entry used to hold, a tracking region left behind in a pane the entry had moved out of.
Those need a sequence nobody thought to write down.

This drives the same fake with sequences Hypothesis chooses, and asserts the properties that must
hold whatever the order (:class:`EditTrailMachine`). The examples stay: a shrunk counterexample says
what broke, not what it was for.

The properties are about edit_trail's own consistency, not about Sublime: they are checked against
the fake, so they cannot notice the editor changing underneath. ``st_tests/`` is what covers that.

Hypothesis requires a newer Python than the plugin's 3.8 floor, so ``just test-py38`` (which installs
only pytest) skips this module rather than failing to import it.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

hypothesis = pytest.importorskip("hypothesis", reason="dev-only dependency, absent on the 3.8 floor")

from hypothesis import HealthCheck, settings  # noqa: E402 - guarded by the importorskip above
from hypothesis import strategies as st  # noqa: E402
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule  # noqa: E402

import edit_trail  # noqa: E402
from tests.conftest import HUNDRED_LINE_TEXT, FakeView, FakeWindow, plugin_settings, run_timeouts  # noqa: E402

if TYPE_CHECKING:
    from tests.conftest import FakeBuffer

OPEN_WINDOW_COUNT = 2
"""How many windows a run works in. Two, because histories are per window and tabs move between them."""

_TEMP_FILE_DIR = Path(tempfile.mkdtemp(prefix="edit_trail_props_"))
atexit.register(shutil.rmtree, _TEMP_FILE_DIR, ignore_errors=True)

SAVED_FILE_PATHS = [str(_TEMP_FILE_DIR / name) for name in ("alpha.txt", "bravo.txt", "charlie.txt")]
"""Files that really exist on disk, so that reopening a closed entry can find one (``is_reachable``)."""

for saved_file_path in SAVED_FILE_PATHS:
    Path(saved_file_path).write_text(HUNDRED_LINE_TEXT, encoding="utf-8")

SHRUNK_FILE_TEXT = "only\ntwo lines"
"""What a refill from disk shrinks a file to, leaving regions past the new end."""

any_window_number = st.integers(min_value=0, max_value=OPEN_WINDOW_COUNT - 1)
"""Which of the open windows a rule acts in. A number, because ``close_window`` replaces the object."""

any_file_to_open = st.sampled_from([*SAVED_FILE_PATHS, None])
"""A file on disk, or None for a buffer that has never been saved and so has no path to reopen from."""

any_row_in_the_file = st.integers(min_value=0, max_value=99)
"""Somewhere to type. HUNDRED_LINE_TEXT is 100 lines, so every row exists."""


class EditTrailMachine(RuleBasedStateMachine):
    """Drive the plugin with random editing, navigation and window sequences.

    Rules are the things a user does that the plugin reacts to. Invariants are the statements that
    have to be true between any two of them; the more expensive sweeps that would change the state
    they are checking run once in :meth:`teardown` instead.
    """

    views = Bundle("views")

    def __init__(self) -> None:
        super().__init__()
        plugin_settings.values.clear()
        plugin_settings.callbacks.clear()
        run_timeouts()
        edit_trail.plugin_loaded()
        self.windows = [FakeWindow() for _ in range(OPEN_WINDOW_COUNT)]
        self.listener = edit_trail.EditTrailListener()
        self.opened_views: list[FakeView] = []
        self.standing_on: dict[int, edit_trail.Entry] = {}
        """Window id to the entry navigation last put the cursor on there. An index that points at
        an entry which is not this one was moved there by something other than a press - a drop, a
        trim - so the user has not been shown that entry, which is what :meth:`teardown` checks."""

    # Helpers

    def _window(self, window_number: int) -> FakeWindow:
        return self.windows[window_number]

    def _buffer_showing(self, file_name: str | None) -> FakeBuffer | None:
        """The buffer of a live view of this file, so reopening a file shares its text as Sublime does."""
        if file_name is None:
            return None
        return next((v.shared_buffer for v in self.opened_views if v.valid and v.path == file_name), None)

    def _entries(self) -> list[edit_trail.Entry]:
        return [entry for history in edit_trail._State.histories.values() for entry in history.entries]

    def _press(self, window: FakeWindow, step: int) -> edit_trail.Entry | None:
        """One press of back or forward. Returns the entry it showed, if it showed one."""
        history = edit_trail._State.histories.get(window.id())
        before = None if history is None else history.index
        edit_trail._navigate(window, step)
        if history is None or history.index == before or history.index >= len(history.entries):
            return None
        shown = history.entries[history.index]
        self.standing_on[window.id()] = shown
        return shown

    def _surviving_views(self) -> list[FakeView]:
        """Only views that still exist. A destroyed view takes its regions with it, so what the fake
        still holds in one is not a leak: the plugin could not erase them there even if it tried."""
        return [view for view in self.opened_views if view.valid]

    # Rules

    @rule(target=views, window_number=any_window_number, file_name=any_file_to_open)
    def open_view(self, window_number: int, file_name: str | None) -> FakeView:
        """Open a file, or a new unsaved buffer, and let the plugin reattach any entries to it."""
        view = FakeView(
            self._window(window_number), file_name, HUNDRED_LINE_TEXT, buffer=self._buffer_showing(file_name)
        )
        self.opened_views.append(view)
        self.listener.on_load(view)
        return view

    @rule(target=views, window_number=any_window_number)
    def open_unsaved_buffer(self, window_number: int) -> FakeView:
        """A new file that has never been saved: the entry with no path, which drops when its last
        view closes rather than being reopenable. A rule of its own because that is the case the
        drop-and-reindex paths need, and one file in four from ``open_view`` was too rare to reach it."""
        view = FakeView(self._window(window_number), None, HUNDRED_LINE_TEXT)
        self.opened_views.append(view)
        return view

    @rule(target=views, view=views)
    def clone_view(self, view: FakeView) -> FakeView:
        """A split pane: a second view of one buffer, in the same window."""
        if not view.valid:
            return view
        clone = FakeView(view.owning_window, view.path, buffer=view.shared_buffer)
        self.opened_views.append(clone)
        self.listener.on_load(clone)
        return clone

    @rule(view=views, row=any_row_in_the_file)
    def type_a_character(self, view: FakeView, row: int) -> None:
        if view.valid:
            view.type_at_row(row, "x")

    @rule(window_number=any_window_number, step=st.sampled_from([-1, 1]))
    def navigate(self, window_number: int, step: int) -> None:
        self._press(self._window(window_number), step)

    @rule(window_number=any_window_number, press_count=st.integers(min_value=1, max_value=5))
    def walk_back(self, window_number: int, press_count: int) -> None:
        """Hold the back key: several presses in a row, which is how the index gets far from the head."""
        for _ in range(press_count):
            self._press(self._window(window_number), -1)

    @rule(window_number=any_window_number)
    def close_what_i_am_looking_at(self, window_number: int) -> None:
        """Go back, then close the file you landed in, every pane of it.

        The entry being dropped is then the one the index points at, which is the case
        ``History.remove`` returns to the head for. Closing every view of the buffer is what makes
        an unsaved one drop rather than move to a surviving pane.
        """
        history = edit_trail._State.histories.get(self._window(window_number).id())
        if history is None or not 0 <= history.index < len(history.entries):
            return
        # The plugin holds views as sublime.View; in these tests every one of them is a FakeView.
        looking_at = cast("FakeView | None", history.entries[history.index].view)
        if looking_at is None or not looking_at.valid:
            return
        for pane in looking_at.shared_buffer.views():
            if pane.valid and pane in pane.owning_window._views:
                pane.close()

    @rule(view=views)
    def close_view(self, view: FakeView) -> None:
        if view.valid and view in view.owning_window._views:
            view.close()

    @rule(view=views, keep_regions=st.booleans(), shrink_the_file=st.booleans())
    def refill_from_disk(self, view: FakeView, keep_regions: bool, shrink_the_file: bool) -> None:
        """A revert or a reload: the whole buffer is replaced, and regions may or may not survive."""
        if view.valid:
            view.revert(SHRUNK_FILE_TEXT if shrink_the_file else HUNDRED_LINE_TEXT, keep_regions=keep_regions)

    @rule(view=views)
    def go_idle(self, view: FakeView) -> None:
        """Let the throttled anchor refresh run, as the editor does between keystrokes."""
        run_timeouts()

    @rule(max_entries=st.integers(min_value=1, max_value=6))
    def change_the_history_limit(self, max_entries: int) -> None:
        plugin_settings.set("max_entries", max_entries)

    @rule(view=views, window_number=any_window_number)
    def drag_tab_to_another_window(self, view: FakeView, window_number: int) -> None:
        """Drag a tab into another window, which Sublime reports through no hook at all."""
        moved_to = self._window(window_number)
        if not view.valid or view.owning_window is moved_to or view not in view.owning_window._views:
            return
        view.owning_window._views.remove(view)
        if view.owning_window.active is view:
            view.owning_window.active = None
        view.owning_window = moved_to
        moved_to._views.append(view)

    @rule(window_number=any_window_number)
    def close_window(self, window_number: int) -> None:
        """Close a window, taking its views with it without an on_pre_close for any of them."""
        window = self._window(window_number)
        self.listener.on_pre_close_window(window)
        for view in list(window._views):
            view.valid = False
            window._views.remove(view)
        self.windows[window_number] = FakeWindow()

    # Invariants

    @invariant()
    def index_is_a_position_in_the_history(self) -> None:
        """``len(entries)`` is the head; anything outside 0..len is a slot navigation cannot use."""
        for history in edit_trail._State.histories.values():
            assert 0 <= history.index <= len(history.entries)

    @invariant()
    def the_index_rests_only_on_an_entry_the_user_was_shown(self) -> None:
        """Between press_count the index is either at the head or on the entry the last press showed.

        This is the stranding condition stated without having to navigate to find it. An index left
        on any other entry means something moved it - a drop, a trim - onto an entry the user has
        never been taken to, and ``target = index + step`` then offers that entry to neither
        direction. ``History.remove`` and ``History.trim`` return to the head for exactly this
        reason, and :meth:`teardown` walks the history to show the consequence when they do not.
        """
        for window_id, history in edit_trail._State.histories.items():
            if 0 <= history.index < len(history.entries):
                assert history.entries[history.index] is self.standing_on.get(window_id), (
                    "the index was moved onto an entry no press ever showed"
                )

    @invariant()
    def history_is_capped(self) -> None:
        for history in edit_trail._State.histories.values():
            assert len(history.entries) <= edit_trail._Config.max_entries

    @invariant()
    def region_keys_are_unique(self) -> None:
        keys = [entry.key for entry in self._entries()]
        assert len(keys) == len(set(keys))

    @invariant()
    def every_region_belongs_to_the_entry_tracking_it(self) -> None:
        """No entry tracking in two views at once, and no region left behind in a view it moved out of."""
        tracked_by = {entry.key: entry.view for entry in self._entries()}
        for view in self._surviving_views():
            for key in view.regions:
                if not key.startswith(edit_trail._State.region_prefix):
                    continue
                assert key in tracked_by, f"region {key} left behind with no entry"
                assert tracked_by[key] is view, f"region {key} is in a view its entry is not attached to"

    @invariant()
    def attached_entries_agree_with_their_view(self) -> None:
        """The cached buffer id is what tells a split pane of this file from another file."""
        for entry in self._entries():
            view = cast("FakeView | None", entry.live_view())
            if view is not None:
                assert entry.buffer_id == view.buffer_id()
                assert entry.key in view.regions

    # End of run

    def teardown(self) -> None:
        """Sweeps that move the state they check, so they run once the random sequence is over."""
        try:
            for window in self.windows:
                self._every_entry_can_be_navigated_to(window)
            self._every_surviving_entry_is_reachable()
            self._unload_leaves_no_regions_behind()
        finally:
            edit_trail.plugin_unloaded()

    def _every_entry_can_be_navigated_to(self, window: FakeWindow) -> None:
        """Walking back to the oldest and then forward to the newest must show every entry.

        An entry no press ever shows is stranded, which is what the index arithmetic in
        ``History.remove`` and ``History.trim`` exists to prevent: leaving the index on the slot a
        dropped entry held makes going back skip the entry that slid into it and going forward step
        over it, so it stays unreachable until the next edit.

        The entry the index already points at counts as reached only if a press is what put the
        cursor there, which is the difference between "you are on it" and "the index was left on it
        by something you never saw". The window is left with no active view before every press,
        because an entry at the cursor is skipped on purpose and a whole history can legitimately
        collapse onto one spot (a revert that shrank the file puts every entry in it on the same two
        lines). With no cursor nothing is skipped, so what is left is the arithmetic.
        """
        history = edit_trail._State.histories.get(window.id())
        if history is None:
            return
        reached: set[edit_trail.Entry] = set()
        on_now = history.entries[history.index] if history.index < len(history.entries) else None
        if on_now is not None and on_now is self.standing_on.get(window.id()):
            reached.add(on_now)
        for step in (-1, 1):
            for _ in range(len(history.entries) + 1):
                window.active = None
                shown = self._press(window, step)
                if shown is not None:
                    reached.add(shown)
        stranded = [entry for entry in history.entries if entry not in reached]
        assert not stranded, f"{len(stranded)} of {len(history.entries)} entries cannot be navigated to"

    def _every_surviving_entry_is_reachable(self) -> None:
        """Navigation drops what it cannot show, so what is left must be showable."""
        for entry in self._entries():
            assert entry.is_reachable()

    def _unload_leaves_no_regions_behind(self) -> None:
        edit_trail.plugin_unloaded()
        for view in self._surviving_views():
            leaked = [key for key in view.regions if key.startswith(edit_trail._State.region_prefix)]
            assert leaked == [], f"unload left {leaked} in a view"


EditTrailMachine.TestCase.settings = settings(
    # Deliberately above Hypothesis's defaults (100 x 50): the sequences that matter here need an
    # entry to be navigated to and then have its last view closed, or max_entries lowered while the
    # index sits in the range about to be trimmed. Both were reached reliably at 1000 examples and
    # not at 200, measured by mutating History.remove and History.trim and checking the run failed.
    max_examples=1000,
    stateful_step_count=50,
    # The autouse fresh_plugin fixture resets the plugin once per test function; this machine resets
    # it per sequence in __init__, which is what the health check is warning about.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)

TestEditTrailMachine = EditTrailMachine.TestCase
