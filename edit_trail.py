"""
EditTrail: edit location history for Sublime Text 4.

Like VS Code's "Go to Last Edit Location": remembers where you edited, across
files, and steps back and forward through those places. Unlike the built-in
``jump_back`` / ``jump_forward``, plain cursor movement and go-to-definition
hops are not recorded, only edits. Unlike ``prev_modification`` /
``next_modification``, it follows the order you edited in and crosses files.

Commands (window commands)
--------------------------
``edit_trail_back``
    Move to the previous (older) edit location.
``edit_trail_forward``
    Move to the next (newer) edit location, after having gone back.

No keys are bound by default. See ``Default.sublime-keymap`` for examples.

Settings (``EditTrail.sublime-settings``)
-----------------------------------------
``max_entries``
    Maximum edit locations kept per window. Default 50.
``merge_line_distance``
    Edits within this many lines of the newest entry, in the same view, update
    that entry instead of adding a new one. Default 5.
``debug``
    Print each recorded or ignored modification to the console. Default false.

Settings are read once at load and re-read when the settings file changes, so
the keystroke hot path never touches the settings API.

How recording works
-------------------
* Each window has its own :class:`History`: a list of :class:`Entry`, oldest
  first, capped at ``max_entries``.
* On every modification of an ordinary editor view, the primary cursor is
  taken as the edit location. The cursor is a good proxy for where the user
  typed. Programmatic edits elsewhere in the file, such as a formatter on save,
  leave the cursor where it was, so they merge into the newest entry instead
  of adding noise.
* Nearby edits merge (see ``merge_line_distance``), so typing a paragraph
  produces one entry, not one per line.
* Panels, the console, input widgets and scratch views (Terminus terminals,
  build and result views, previews) are ignored. See :func:`_is_trackable`.
* Modifications that are not the user editing are ignored
  (:meth:`EditTrailListener.on_modified`). Sublime also reports a modification
  when it fills a view with file content, e.g. restoring a project's open
  files or reloading a file changed on disk. Those views are still loading or
  have no unsaved changes afterwards. Changes to background views (save-all
  whitespace trimming, format on save, multi-file refactors) are not where the
  user is typing either. So a modification is recorded only when the view is
  not loading, has unsaved changes, and is the window's active view. The cost:
  undoing back to the saved state, and edits a plugin makes in other files,
  leave no entry.

How positions stay correct
--------------------------
* While a file is open, an entry's position is a hidden, zero-width region
  (``View.add_regions``). Sublime shifts regions as text is inserted or deleted
  before them, so the location stays on the same text.
* When the view closes, the entry keeps the last known row/column and the file
  path (:meth:`Entry.detach`). Navigating to it reopens the file at that
  position, and the entry re-attaches to the new view once the file has loaded
  (:meth:`EditTrailListener.on_load`).
* Entries for an unsaved buffer move to another open view (clone) of the same
  buffer when one view closes, and are dropped once no view of the buffer is
  left, because there is nothing to reopen.
* Entries whose file no longer exists on disk are dropped when navigation
  reaches them, and navigation carries on in the same direction.

Windows
-------
A trail never crosses windows. Each window (typically one project) has its own
history, and navigation only ever focuses views in the window the command ran
in. The cases that could otherwise leak between windows are handled explicitly:

* A tab dragged to another window after its edit: the entry is dropped from
  the old window's history when navigation reaches it (:func:`_go`).
* A closed file reopened in a different window: only the history of the window
  it opened in re-attaches (:meth:`EditTrailListener.on_load`).
* An unsaved buffer with clones in several windows: entries only move to a
  clone in the same window (:func:`_other_view_of_buffer`).

Navigation semantics
--------------------
* ``History.index`` points at the entry last navigated to, or equals
  ``len(entries)`` when not navigating ("at the head").
* A new edit resets the index to the head. Nothing is truncated, so going back
  always walks the full chronological history.
* The first ``edit_trail_back`` from the head skips the newest entry when the
  cursor is already near it, so one press always goes somewhere new.

Performance
-----------
* ``on_modified`` runs synchronously on every keystroke, so it is kept to a
  handful of cheap API calls: a per-view cached trackable check,
  ``is_loading``, ``is_dirty``, ``active_view``, ``sel``, ``is_valid`` and
  ``get_regions`` for the newest entry, two ``rowcol`` and one
  ``add_regions``. ``View.file_name`` and settings are not read there.
* The synchronous handler is deliberate. Recording and navigation then share
  one thread, so the history needs no locks, and an edit immediately followed
  by ``edit_trail_back`` is already recorded.
* Work on view close and file load is bounded by ``max_entries`` per window.
* History is in memory only and does not survive a restart.

Compatibility
-------------
Requires Sublime Text build 4050 or later (``View.element``, ``View.buffer``).
``.python-version`` selects the modern plugin host, so this module is written
for Python 3.8 syntax: annotations are postponed with
``from __future__ import annotations`` and no 3.9+ runtime features are used.
"""

from __future__ import annotations

import time
from pathlib import Path

import sublime
import sublime_plugin

SETTINGS_FILE = "EditTrail.sublime-settings"
DEFAULT_MAX_ENTRIES = 50
DEFAULT_MERGE_LINE_DISTANCE = 5


class _Config:
    """Settings snapshot, refreshed on load and whenever the settings change.

    Kept as plain attributes so the hot path reads a Python attribute rather
    than calling into the settings API.
    """

    max_entries: int = DEFAULT_MAX_ENTRIES
    merge_line_distance: int = DEFAULT_MERGE_LINE_DISTANCE
    debug: bool = False


def _debug(message: str, view: sublime.View) -> None:
    """Print a diagnostic line to the Sublime console when ``debug`` is on."""
    if _Config.debug:
        print(f"EditTrail: {message}: {view.file_name() or view.name() or view.id()}")  # noqa: T201 - console output is the point


class _State:
    """Module-level mutable state, grouped so tests can reset it in one place.

    Attributes:
        histories: Window id to that window's History.
        trackable: View id to the cached result of _is_trackable. A view's kind
            (panel, widget, terminal) does not change after creation, and the
            check costs several API round trips, so it is computed once.
        key_counter: Source of unique region keys.
        region_prefix: Region key namespace for this plugin load, so a reload
            never collides with regions left behind by a previous load.
    """

    histories: dict[int, History] = {}  # noqa: RUF012 - deliberate module-wide state
    trackable: dict[int, bool] = {}  # noqa: RUF012 - deliberate module-wide state
    key_counter: int = 0
    region_prefix: str = "edit_trail_"


def _new_region_key() -> str:
    """Return a region key unique for the lifetime of this plugin load."""
    _State.key_counter += 1
    return f"{_State.region_prefix}{_State.key_counter}"


class Entry:
    """One remembered edit location.

    Attributes:
        view: The live view holding the tracking region, or None while the file
            is closed.
        file_name: Absolute path of the file, or None for an unsaved buffer.
            Refreshed on detach, so a buffer that was saved (or saved under a
            new name) after the edit is followed.
        key: Region key of the hidden region tracking the position.
        row: Last known 0-based row. Authoritative only while detached; while
            attached the region is authoritative and row/col are refreshed on
            detach.
        col: Last known 0-based column, see ``row``.
    """

    __slots__ = ("col", "file_name", "key", "row", "view")

    def __init__(self, view: sublime.View, point: int) -> None:
        self.view: sublime.View | None = view
        self.file_name: str | None = view.file_name()
        self.key = _new_region_key()
        self.row, self.col = view.rowcol(point)
        self._place(view, point)

    def _place(self, view: sublime.View, point: int) -> None:
        """(Re)place the hidden tracking region at ``point`` in ``view``."""
        view.add_regions(self.key, [sublime.Region(point)], flags=sublime.HIDDEN)

    def update(self, point: int) -> None:
        """Move the entry to ``point`` in its attached view.

        Called on the keystroke hot path, so it does not refresh ``file_name``;
        :meth:`detach` does that before the path is ever needed.
        """
        view = self.live_view()
        if view is not None:
            self._place(view, point)

    def live_view(self) -> sublime.View | None:
        """Return the attached view if it still exists, else None."""
        if self.view is not None and self.view.is_valid():
            return self.view
        return None

    def point(self) -> int | None:
        """Return the current text point in the attached view, or None."""
        view = self.live_view()
        if view is None:
            return None
        regions = view.get_regions(self.key)
        if not regions:
            return None
        return regions[0].b

    def detach(self) -> None:
        """Stop tracking in the live view, remembering the path and row/col.

        Must run before the view closes (``on_pre_close``), while the view and
        its regions can still be read.
        """
        view = self.live_view()
        if view is not None:
            point = self.point()
            if point is not None:
                self.row, self.col = view.rowcol(point)
            self.file_name = view.file_name()
            view.erase_regions(self.key)
        self.view = None

    def attach(self, view: sublime.View) -> None:
        """Start tracking in ``view`` at the remembered row/col.

        The position is clamped to the buffer, because the file may have
        changed on disk while it was closed.
        """
        self.view = view
        last_row = view.rowcol(view.size())[0]
        line = view.line(view.text_point(min(self.row, last_row), 0))
        self._place(view, min(line.a + self.col, line.b))

    def move_to_clone(self, closing_view: sublime.View, clone: sublime.View) -> None:
        """Move tracking from a closing view to another view of its buffer.

        Views of one buffer share the same text, so the point is valid in both.
        """
        point = self.point()
        closing_view.erase_regions(self.key)
        if point is None:
            self.attach(clone)
        else:
            self.view = clone
            self._place(clone, point)

    def is_reachable(self) -> bool:
        """Return True if navigation can still get to this entry."""
        return self.live_view() is not None or bool(self.file_name)


class History:
    """Chronological edit locations for one window.

    Attributes:
        entries: Entries, oldest first.
        index: Position of the entry last navigated to. ``len(entries)`` means
            "at the head", i.e. not navigating.
    """

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.index = 0

    def record(self, view: sublime.View, point: int) -> None:
        """Record an edit at ``point`` in ``view``.

        Merges into the newest entry when it is in the same view and within
        ``merge_line_distance`` lines, otherwise appends and trims the oldest
        entries beyond ``max_entries``. Always resets navigation to the head.
        """
        if self.entries:
            newest = self.entries[-1]
            newest_view = newest.live_view()
            newest_point = newest.point()
            if (
                newest_view is not None
                and newest_point is not None
                and newest_view.id() == view.id()
                and _near(view, newest_point, point)
            ):
                newest.update(point)
                self.index = len(self.entries)
                return

        self.entries.append(Entry(view, point))
        overflow = len(self.entries) - max(1, _Config.max_entries)
        if overflow > 0:
            for entry in self.entries[:overflow]:
                entry.detach()
            del self.entries[:overflow]
        self.index = len(self.entries)

    def remove(self, entry: Entry) -> None:
        """Drop ``entry``, keeping ``index`` on the same logical position."""
        position = self.entries.index(entry)
        entry.detach()
        del self.entries[position]
        if position < self.index:
            self.index -= 1

    def prune(self) -> None:
        """Drop entries that navigation can no longer reach."""
        for entry in [e for e in self.entries if not e.is_reachable()]:
            self.remove(entry)
        self.index = min(self.index, len(self.entries))

    def clear(self) -> None:
        """Detach every entry, erasing their regions from open views."""
        for entry in self.entries:
            entry.detach()
        self.entries = []
        self.index = 0


def _is_trackable(view: sublime.View) -> bool:
    """Return True for ordinary editor views whose edits should be recorded.

    Excludes panels, the console and input widgets (``element()`` is not None),
    views flagged ``is_widget``, and scratch views, which covers Terminus
    terminals, build and "Find Results" style output, and plugin previews that
    are written programmatically.

    The result is cached per view id in ``_State.trackable``.
    """
    cached = _State.trackable.get(view.id())
    if cached is not None:
        return cached
    trackable = not (view.element() is not None or view.settings().get("is_widget") or view.is_scratch())
    _State.trackable[view.id()] = trackable
    return trackable


def _near(view: sublime.View, point_a: int, point_b: int) -> bool:
    """Return True if two points in ``view`` are within merge_line_distance lines."""
    return abs(view.rowcol(point_a)[0] - view.rowcol(point_b)[0]) <= _Config.merge_line_distance


def _cursor_near_entry(window: sublime.Window, entry: Entry) -> bool:
    """Return True if the active view is the entry's view with the cursor near it."""
    view = window.active_view()
    entry_view = entry.live_view()
    point = entry.point()
    if view is None or entry_view is None or point is None or entry_view.id() != view.id():
        return False
    selection = view.sel()
    if len(selection) == 0:
        return False
    return _near(view, selection[0].b, point)


def _in_window(view: sublime.View, window: sublime.Window) -> bool:
    """Return True if ``view`` currently belongs to ``window``."""
    view_window = view.window()
    return view_window is not None and view_window.id() == window.id()


def _other_view_of_buffer(view: sublime.View) -> sublime.View | None:
    """Return another open view (clone) of ``view``'s buffer in the same window, or None.

    Clones in other windows are not used, so entries never move across windows.
    """
    window = view.window()
    if window is None:
        return None
    for other in view.buffer().views():
        if other.id() != view.id() and other.is_valid() and _in_window(other, window):
            return other
    return None


def _go(window: sublime.Window, history: History, entry: Entry) -> bool:
    """Show ``entry``: focus its view and place the cursor, or reopen its file.

    Navigation never leaves ``window``: an entry whose view has since moved to
    another window (a dragged tab) is dropped rather than followed.

    Returns:
        True if the entry was shown or its file is being opened. False if it
        cannot be shown (the file was deleted, or its view is now in another
        window), in which case it has been removed from ``history``.
    """
    view = entry.live_view()
    if view is not None and not _in_window(view, window):
        history.remove(entry)
        return False
    if view is None and entry.file_name:
        found = window.find_open_file(entry.file_name)
        if found is not None and not found.is_loading():
            entry.attach(found)
            view = found

    if view is not None:
        point = entry.point()
        if point is None:
            # Region lost, e.g. erased by another plugin: rebuild from row/col.
            entry.attach(view)
            point = entry.point()
        if point is not None:
            window.focus_view(view)
            view.sel().clear()
            view.sel().add(sublime.Region(point))
            view.show_at_center(point)
            return True

    if entry.file_name and Path(entry.file_name).is_file():
        # ENCODED_POSITION parses ":row:col" from the right, so Windows drive
        # letters ("C:\...") are safe. The entry re-attaches in on_load.
        window.open_file(f"{entry.file_name}:{entry.row + 1}:{entry.col + 1}", sublime.ENCODED_POSITION)
        return True

    history.remove(entry)
    return False


def _navigate(window: sublime.Window, step: int) -> None:
    """Move ``step`` (-1 back, +1 forward) through the window's history.

    Targets that cannot be shown are dropped and skipped, continuing in the
    same direction. Reports the outcome in the status bar.
    """
    history = _State.histories.get(window.id())
    if history is not None:
        history.prune()
    if history is None or not history.entries:
        sublime.status_message("EditTrail: no edit locations recorded")
        return

    if step < 0:
        target = history.index - 1
        if (
            history.index >= len(history.entries)
            and target >= 0
            and _cursor_near_entry(window, history.entries[target])
        ):
            target -= 1
    else:
        target = history.index + 1

    while 0 <= target < len(history.entries):
        history.index = target
        if _go(window, history, history.entries[target]):
            sublime.status_message(f"EditTrail: edit location {target + 1}/{len(history.entries)}")
            return
        # The unreachable entry was removed. Going back, the next older entry
        # is at target - 1. Going forward, the next newer one slid into target.
        if step < 0:
            target -= 1

    history.index = min(history.index, len(history.entries))
    sublime.status_message("EditTrail: no older edit locations" if step < 0 else "EditTrail: no newer edit locations")


class EditTrailListener(sublime_plugin.EventListener):
    """Records edits and keeps entries attached as views close and reopen."""

    def on_modified(self, view: sublime.View) -> None:
        """Record the primary cursor as an edit location. Keystroke hot path.

        Sublime calls this for any change to the buffer, not only user edits.
        Each guard below rejects one kind of change the user did not type:

        * ``is_loading``: content arriving while the file loads.
        * ``not is_dirty``: the view matches the file on disk, so the change
          was a load, restore or reload (or an undo back to the saved state).
        * not the active view: a plugin or save-all changing a background view.

        Checks run cheapest and most selective first.
        """
        if not _is_trackable(view):
            return
        window = view.window()
        if window is None:
            return
        if view.is_loading() or not view.is_dirty():
            _debug("ignored change with no unsaved edits (load, reload or undo to saved)", view)
            return
        active = window.active_view()
        if active is None or active.id() != view.id():
            _debug("ignored change in a background view", view)
            return
        selection = view.sel()
        if len(selection) == 0:
            return
        if _Config.debug:  # guarded so the message is not built on every keystroke
            _debug(f"recorded edit at line {view.rowcol(selection[0].b)[0] + 1}", view)
        history = _State.histories.get(window.id())
        if history is None:
            history = _State.histories[window.id()] = History()
        history.record(view, selection[0].b)

    def on_pre_close(self, view: sublime.View) -> None:
        """Detach entries from a closing view, or hand them to a clone."""
        view_id = view.id()
        _State.trackable.pop(view_id, None)
        clone: sublime.View | None = None
        clone_looked_up = False
        for history in _State.histories.values():
            for entry in history.entries:
                entry_view = entry.view
                if entry_view is None or entry_view.id() != view_id:
                    continue
                if not clone_looked_up:
                    clone = _other_view_of_buffer(view)
                    clone_looked_up = True
                if clone is None:
                    entry.detach()
                else:
                    entry.move_to_clone(view, clone)

    def on_load(self, view: sublime.View) -> None:
        """Re-attach detached entries when their file is opened again.

        Only the history of the window the file opened in is considered, so
        opening the same file in another window never pulls entries across.
        """
        file_name = view.file_name()
        window = view.window()
        if not file_name or window is None:
            return
        history = _State.histories.get(window.id())
        if history is None:
            return
        for entry in history.entries:
            if entry.view is None and entry.file_name == file_name:
                entry.attach(view)

    def on_pre_close_window(self, window: sublime.Window) -> None:
        """Forget a closing window's history."""
        history = _State.histories.pop(window.id(), None)
        if history is not None:
            history.clear()


class EditTrailBackCommand(sublime_plugin.WindowCommand):
    """Go to the previous (older) edit location."""

    def run(self, **_kwargs: object) -> None:
        """Run the command. Takes no arguments; ``**_kwargs`` matches the base signature."""
        _navigate(self.window, -1)


class EditTrailForwardCommand(sublime_plugin.WindowCommand):
    """Go to the next (newer) edit location."""

    def run(self, **_kwargs: object) -> None:
        """Run the command. Takes no arguments; ``**_kwargs`` matches the base signature."""
        _navigate(self.window, 1)


def _read_settings() -> None:
    """Copy the settings into _Config, falling back to defaults on bad values."""
    settings = sublime.load_settings(SETTINGS_FILE)
    max_entries = settings.get("max_entries", DEFAULT_MAX_ENTRIES)
    merge_distance = settings.get("merge_line_distance", DEFAULT_MERGE_LINE_DISTANCE)
    _Config.max_entries = max_entries if isinstance(max_entries, int) and max_entries > 0 else DEFAULT_MAX_ENTRIES
    _Config.merge_line_distance = (
        merge_distance if isinstance(merge_distance, int) and merge_distance >= 0 else DEFAULT_MERGE_LINE_DISTANCE
    )
    _Config.debug = settings.get("debug", False) is True


def plugin_loaded() -> None:
    """Sublime entry point: namespace region keys and load settings."""
    _State.region_prefix = f"edit_trail_{time.time_ns()}_"
    _read_settings()
    sublime.load_settings(SETTINGS_FILE).add_on_change("edit_trail", _read_settings)


def plugin_unloaded() -> None:
    """Sublime exit point: erase all tracking regions and stop watching settings."""
    for history in _State.histories.values():
        history.clear()
    _State.histories.clear()
    _State.trackable.clear()
    sublime.load_settings(SETTINGS_FILE).clear_on_change("edit_trail")
