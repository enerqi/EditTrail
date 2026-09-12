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
    that entry instead of adding a new one. Default 5. The same distance also
    decides which entries navigation treats as "where the cursor already is"
    and skips (see `Navigation semantics`), so raising it to record fewer
    locations also widens the band around the cursor that ``edit_trail_back``
    and ``edit_trail_forward`` step over.
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
* A revert, or a reload after the file changed on disk, replaces the whole
  buffer. Sublime may drop the tracking regions or leave them past the new end
  of file, so entries re-anchor then, falling back to the remembered
  row/column (:func:`_reanchor`).
* That fallback row/column is only useful if it is current, and the region
  moves under it as the text around it changes. So when typing pauses, every
  entry copies its region's position back into its row/column
  (:func:`_refresh_anchors`). Off the keystroke path, once per burst.

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
* Every press goes somewhere visibly different: entries near the cursor,
  within ``merge_line_distance`` lines of it, are skipped. That covers the
  newest entry on the first ``edit_trail_back`` from the head, and entries that
  collapsed onto one spot when the text between them was deleted.
* The commands are disabled (greyed out in the palette and menus) when there
  is nothing older, or nothing newer, to go to.

Performance
-----------
* ``on_modified`` runs synchronously on every keystroke, so it is kept to a
  handful of cheap API calls: ``is_scratch``, ``window``, ``is_loading``,
  ``is_dirty``, ``active_view``, the primary selection (indexed directly, not
  ``len`` then index), then ``get_regions`` for the newest entry. Seven, and
  that is the whole cost of typing on once Sublime has shifted that entry's
  region onto the cursor. When the region does have to move, two ``rowcol``
  and one ``add_regions`` follow: ten. The panel/widget check is cached per
  view, the modified view is known to be valid so no ``is_valid`` is needed,
  and neither ``View.file_name`` nor the settings API is touched. The work does
  not grow with file size or history length.
* Two round trips are made off that per-keystroke path. ``View.file_name`` when
  a new entry is created (:class:`Entry`), which happens when the location is
  new rather than on every key. And one ``set_timeout`` per burst of typing to
  schedule :func:`_refresh_anchors`, which then costs three round trips per
  attached entry, while the editor is idle.
* ``is_enabled`` on the commands, which Sublime calls on every palette and
  menu redraw, is a dictionary lookup with no API calls.
* Navigation is lazy: a keypress never sweeps the history looking for entries
  that died. They are dropped as the walk reaches them, so the cost is
  proportional to the entries actually visited, not to ``max_entries``.
* Every API call is a round trip to the editor process, roughly a thousand
  times the cost of a Python attribute lookup. So the code minimises API calls
  and does not cache module or attribute lookups into locals, which would
  save nothing measurable at the cost of readability.
* Memory is bounded: at most ``max_entries`` slotted :class:`Entry` objects
  and one slotted :class:`History` per window, plus one cached id per ordinary
  editor view modified (dropped when the view closes, or when its window does;
  panels and widgets are never cached, see :func:`_is_trackable`).
  ``_Config`` and ``_State`` are never instantiated; they are namespaces of
  class attributes, so ``__slots__`` would not apply to them.
* The synchronous handler is deliberate. Recording and navigation then share
  one thread, so the history needs no locks, and an edit immediately followed
  by ``edit_trail_back`` is already recorded. The one deferred piece of work,
  :func:`_refresh_anchors`, is scheduled with ``set_timeout``, which also runs
  on that thread.
* Work on view close and file load is bounded by ``max_entries`` per window.
* History is in memory only and does not survive a restart.

Pitfalls this avoids
--------------------
Each of these is a way an edit-history plugin quietly goes wrong. The fix is
commented at the code that avoids it.

* Caching "is this a scratch view": plugins flip ``is_scratch`` on a live
  view, so it is re-read on every modification (:func:`_is_trackable`).
* Comparing file paths as plain strings: on Windows, Sublime can hand back a
  path cased or separated differently from the one recorded, so re-attaching
  compares ``Path`` objects, whose equality folds both there
  (:meth:`EditTrailListener.on_load`).
* Sweeping the whole history for dead entries on every navigation keypress:
  they are dropped lazily by :func:`_go` instead (:func:`_navigate`).
* Leaving entries anchored in a buffer that was reverted or reloaded from
  disk (:func:`_reanchor`).
* Leaking the per-view kind cache when a window closes without closing each
  tab (:meth:`EditTrailListener.on_pre_close_window`), or by caching views no
  close hook reports at all, such as the find panel (:func:`_is_trackable`).
* Letting an entry's fallback row/column go stale while its region moves, so
  that a buffer refilled from disk re-anchors the entry where it was first
  typed rather than where the edit ended up (:func:`_refresh_anchors`).
* Looking a clone up in the closing view's window and then handing it to
  another window's history, which would leave the entry tracking a view
  navigation from that window can never focus (:func:`_other_view_of_buffer`).
* ``isinstance(True, int)``: a boolean in the settings file would otherwise
  validate as a count (:func:`_read_settings`).
* A lowered ``max_entries`` not applying until the next edit
  (:meth:`History.trim`).
* Rewriting the tracking region on every keystroke when Sublime has already
  shifted it onto the cursor (:meth:`Entry.merge`).
* Reading ``View.file_name`` on the merge path, which runs on every keystroke:
  it is read when an entry is created and refreshed in :meth:`Entry.detach`,
  never in :meth:`Entry.merge`. Reading it at creation and not only on detach
  is what keeps an entry reopenable when its view is invalidated without
  ``on_pre_close``, as happens when a whole window closes.

Compatibility
-------------
Requires Sublime Text build 4107 or later, the first stable Sublime Text 4
release. The newest API used is ``View.buffer`` (build 4083); ``View.element``
and the ``on_reload`` / ``on_revert`` listener hooks arrived in 4050. Builds before 4107 were dev-channel only.
``.python-version`` selects the modern plugin host, which is a real Python 3.8
on stable builds before 4205, so this module must run on 3.8. It is fully
typed all the same: annotations are postponed with
``from __future__ import annotations``, so 3.9+ annotation syntax
(``list[int]``, ``X | None``) is never evaluated, and type aliases that would
need it at runtime live under ``TYPE_CHECKING``. Everything imported from
``typing`` (``ClassVar``, ``Final``, ``Literal``, ``final``) exists in 3.8.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final, Literal, final

import sublime
import sublime_plugin

if TYPE_CHECKING:
    # Type-only aliases. Annotations are postponed (PEP 563), so these are never
    # evaluated at runtime, where 3.8 cannot subscript `tuple` or use `X | Y`.
    Cursor = tuple[sublime.View, int] | None
    """The active view and its primary cursor point, or None."""
    Step = Literal[-1, 1]
    """Navigation direction: -1 back (older), 1 forward (newer)."""

SETTINGS_FILE: Final = "EditTrail.sublime-settings"
DEFAULT_MAX_ENTRIES: Final = 50
DEFAULT_MERGE_LINE_DISTANCE: Final = 5
ANCHOR_REFRESH_DELAY_MS: Final = 500
"""Idle delay before entries refresh their fallback row/column, see _refresh_anchors."""


@final
class _Config:
    """Settings snapshot, refreshed on load and whenever the settings change.

    Kept as plain attributes so the hot path reads a Python attribute rather
    than calling into the settings API. Never instantiated.
    """

    max_entries: ClassVar[int] = DEFAULT_MAX_ENTRIES
    merge_line_distance: ClassVar[int] = DEFAULT_MERGE_LINE_DISTANCE
    debug: ClassVar[bool] = False


def _debug(message: str, view: sublime.View) -> None:
    """Print a diagnostic line to the Sublime console when ``debug`` is on."""
    if _Config.debug:
        print(f"EditTrail: {message}: {view.file_name() or view.name() or view.id()}")  # noqa: T201 - console output is the point


@final
class _State:
    """Module-level mutable state, grouped so tests can reset it in one place. Never instantiated.

    Attributes:
        histories: Window id to that window's History.
        trackable: Ids of the views known to be ordinary editor views, the
            cached panel/widget half of _is_trackable. Whether a view is a
            panel or an input widget is settled when it is created and costs
            two API round trips to check, so it is computed once. Only the
            views that pass are cached, so panels and widgets, which no close
            hook reliably reports, cannot leak into it; see _is_trackable. Ids
            are dropped when the view closes, or when its window does.
        refresh_pending: True while a debounced _refresh_anchors call is
            scheduled, so a burst of typing schedules only one.
        key_counter: Source of unique region keys.
        region_prefix: Region key namespace for this plugin load, so a reload
            never collides with regions left behind by a previous load.
    """

    histories: ClassVar[dict[int, History]] = {}
    trackable: ClassVar[set[int]] = set()
    refresh_pending: ClassVar[bool] = False
    key_counter: ClassVar[int] = 0
    region_prefix: ClassVar[str] = "edit_trail_"


def _new_region_key() -> str:
    """Return a region key unique for the lifetime of this plugin load."""
    _State.key_counter += 1
    return f"{_State.region_prefix}{_State.key_counter}"


@final
class Entry:
    """One remembered edit location.

    Attributes:
        view: The live view holding the tracking region, or None while the file
            is closed.
        file_name: Absolute path of the file, or None for a buffer that has
            never been saved. Recorded when the entry is created and refreshed
            by :meth:`detach`, so it follows a buffer saved, or saved under a
            new name, after the edit. It is recorded at creation as well
            because a view can be invalidated without ``on_pre_close`` (a whole
            window closing), and an entry that never learned its path could
            then never be reopened.
        key: Region key of the hidden region tracking the position.
        row: Last known 0-based row. Authoritative while detached; while
            attached the region is authoritative and row/col are the fallback
            for a region Sublime drops, kept close to the region by
            :meth:`refresh_anchor` and refreshed on detach.
        col: Last known 0-based column, see ``row``.
    """

    __slots__ = ("col", "file_name", "key", "row", "view")
    # Declared types of the slots (bare annotations do not conflict with __slots__).
    view: sublime.View | None
    file_name: str | None
    key: str
    row: int
    col: int

    def __init__(self, view: sublime.View, point: int) -> None:
        self.view = view
        # One round trip, and only when a new location is recorded rather than
        # merged into the newest entry. detach() refreshes it, see the class
        # docstring.
        self.file_name = view.file_name()
        self.key = _new_region_key()
        self.row, self.col = view.rowcol(point)
        self._place(view, point)

    def _place(self, view: sublime.View, point: int) -> None:
        """(Re)place the hidden tracking region at ``point`` in ``view``."""
        view.add_regions(self.key, [sublime.Region(point)], flags=sublime.HIDDEN)

    def merge(self, view: sublime.View, point: int) -> bool:
        """Move this entry to ``point`` if it is attached to ``view`` and near it.

        Keystroke hot path. ``view`` is the view being modified, so it is known
        to be valid and no ``is_valid`` round trip is needed; comparing ids is a
        local call. ``file_name`` is not refreshed here, :meth:`detach` does
        that before the path is ever needed.

        Returns:
            True if the entry was attached to ``view`` within
            ``merge_line_distance`` lines of ``point`` and has been moved there.
        """
        if self.view is None or self.view.id() != view.id():
            return False
        regions = view.get_regions(self.key)
        if not regions:
            return False
        here = regions[0].b
        if here == point:
            # Sublime already shifted the region onto the cursor as the text
            # was inserted, which is the common case while typing. The two
            # rowcol calls and the add_regions below would all be no-ops.
            return True
        if not _near(view, here, point):
            return False
        self._place(view, point)
        return True

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

    def refresh_anchor(self) -> None:
        """Copy the tracking region's position into the remembered row/column.

        Sublime shifts the region as text is inserted and deleted before it, so
        without this the fallback row/column would still be wherever the entry
        was created, and re-anchoring after a buffer was refilled from disk
        (:meth:`reanchor`) or after another plugin erased the region would land
        far from the edit. Called off the keystroke path, see
        :func:`_refresh_anchors`.
        """
        view = self.live_view()
        if view is None:
            return
        regions = view.get_regions(self.key)
        if regions:
            self.row, self.col = view.rowcol(regions[0].b)

    def detach(self) -> None:
        """Stop tracking in the live view, remembering the path and row/col.

        Must run before the view closes (``on_pre_close``), while the view and
        its regions can still be read.
        """
        view = self.live_view()
        if view is not None:
            # Inlined rather than self.point(), which would repeat the is_valid
            # round trip live_view() has just made.
            regions = view.get_regions(self.key)
            if regions:
                self.row, self.col = view.rowcol(regions[0].b)
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

    def reanchor(self, view: sublime.View) -> None:
        """Re-anchor after the whole buffer of ``view`` was replaced (revert, reload).

        Sublime may drop the tracking region, or leave it past the end of a
        file that shrank, when it refills a buffer from disk. A surviving,
        in-range region is still the best answer, and taking it also refreshes
        the remembered row/column. Failing that, the row/column is all there is
        to rebuild from: :func:`_refresh_anchors` is what keeps it close to the
        region, and :meth:`attach` clamps it to the new buffer.
        """
        regions = view.get_regions(self.key)
        if regions and regions[0].b <= view.size():
            self.row, self.col = view.rowcol(regions[0].b)
        else:
            self.attach(view)

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

    __slots__ = ("entries", "index")
    entries: list[Entry]
    index: int

    def __init__(self) -> None:
        self.entries = []
        self.index = 0

    def record(self, view: sublime.View, point: int) -> None:
        """Record an edit at ``point`` in ``view``.

        Merges into the newest entry when it is in the same view and within
        ``merge_line_distance`` lines, otherwise appends and trims the oldest
        entries beyond ``max_entries``. Always resets navigation to the head.
        """
        if self.entries and self.entries[-1].merge(view, point):
            self.index = len(self.entries)
            return

        self.entries.append(Entry(view, point))
        self.trim()
        self.index = len(self.entries)

    def trim(self) -> None:
        """Drop the oldest entries beyond ``max_entries``.

        Also called when the settings change, so lowering ``max_entries``
        applies at once rather than only at the next edit.
        """
        overflow = len(self.entries) - _Config.max_entries  # validated > 0 in _read_settings
        if overflow > 0:
            for entry in self.entries[:overflow]:
                entry.detach()
            del self.entries[:overflow]
            self.index = max(0, self.index - overflow)

    def remove(self, entry: Entry) -> None:
        """Drop ``entry``, keeping ``index`` on the same logical position."""
        position = self.entries.index(entry)
        entry.detach()
        del self.entries[position]
        if position < self.index:
            self.index -= 1

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

    Only the panel/widget half is cached, in ``_State.trackable``, and only for
    the views that pass it: that is settled when the view is created. Panels,
    the console and input widgets fire ``on_modified`` too (every keystroke in
    the find panel does), and Sublime sends no reliable close hook for them,
    nor lists them in ``Window.views()``, so remembering a "no" for them would
    grow the cache for the rest of the session. Recomputing it costs two round
    trips on a path that then does nothing, while ordinary editor views, the
    ones on the keystroke path, still answer from the cache.

    ``is_scratch`` is deliberately not cached, because plugins do flip it on a
    live view, setting it after filling a preview buffer or clearing it to hand
    the buffer to the user. A cached answer would then either keep recording a
    terminal forever or silently record nothing in a real file. It costs one
    round trip, and only for views that got past the cached half.
    """
    view_id = view.id()
    if view_id in _State.trackable:
        return not view.is_scratch()
    if view.element() is not None or view.settings().get("is_widget"):
        return False
    _State.trackable.add(view_id)
    return not view.is_scratch()


def _near(view: sublime.View, point_a: int, point_b: int) -> bool:
    """Return True if two points in ``view`` are within merge_line_distance lines."""
    return abs(view.rowcol(point_a)[0] - view.rowcol(point_b)[0]) <= _Config.merge_line_distance


def _active_cursor(window: sublime.Window) -> Cursor:
    """Return the window's active view and its primary cursor point, or None.

    Looked up once per navigation rather than once per candidate entry.
    """
    view = window.active_view()
    if view is None:
        return None
    try:
        return view, view.sel()[0].b
    except IndexError:
        return None


def _near_cursor(cursor: Cursor, entry: Entry) -> bool:
    """Return True if ``entry`` is in the cursor's view, near the cursor.

    The active view is valid, so matching its id means the entry is attached
    to a live view and no ``is_valid`` round trip is needed.
    """
    if cursor is None:
        return False
    view, cursor_point = cursor
    if entry.view is None or entry.view.id() != view.id():
        return False
    regions = view.get_regions(entry.key)
    return bool(regions) and _near(view, cursor_point, regions[0].b)


def _in_window(view: sublime.View, window: sublime.Window) -> bool:
    """Return True if ``view`` currently belongs to ``window``."""
    view_window = view.window()
    return view_window is not None and view_window.id() == window.id()


def _other_view_of_buffer(view: sublime.View, window_id: int) -> sublime.View | None:
    """Return another open view (clone) of ``view``'s buffer in ``window_id``, or None.

    The window is the one whose history the entry belongs to, not ``view``'s
    own: a tab dragged to another window leaves entries behind in the old
    window's history, and handing one of those a clone in the window the tab
    moved to would put it where that history's navigation can never follow it.
    Clones in other windows are not used, so entries never move across windows.
    """
    for other in view.buffer().views():
        if other.id() != view.id() and other.is_valid():
            other_window = other.window()
            if other_window is not None and other_window.id() == window_id:
                return other
    return None


def _refresh_anchors() -> None:
    """Refresh every attached entry's fallback row/column. Runs when typing pauses.

    An entry's position is a tracking region, which Sublime shifts as the text
    around it changes, but its row/column is only written when it is created or
    detached. Anything that loses the region in between, a revert or reload
    refilling the buffer (:func:`_reanchor`) or another plugin erasing regions
    (:func:`_go`), would otherwise re-anchor the entry wherever it was first
    typed: silently, and possibly hundreds of lines out.

    Doing it per keystroke would cost round trips proportional to the history,
    so :meth:`EditTrailListener.on_modified` schedules this once per burst of
    typing (``ANCHOR_REFRESH_DELAY_MS`` after the first keystroke of the burst)
    and it sweeps every window's history then. ``set_timeout`` runs it on the
    UI thread, the same thread that records and navigates, so the history still
    needs no locks.
    """
    _State.refresh_pending = False
    for history in _State.histories.values():
        for entry in history.entries:
            entry.refresh_anchor()


def _reanchor(view: sublime.View) -> None:
    """Re-anchor every entry tracking ``view`` after its buffer was replaced.

    A revert, or a reload of a file changed on disk, refills the whole buffer.
    ``on_modified`` ignores that (the view is not dirty afterwards), so without
    this the entries would keep regions Sublime may have dropped or left
    dangling past the new end of file, with a fallback row/column last
    refreshed when the entry was created.

    Rare enough to scan every window's history rather than only the view's
    current window: a tab dragged between windows leaves entries behind in the
    old window's history, and they track this view too.
    """
    for history in _State.histories.values():
        for entry in history.entries:
            entry_view = entry.view
            if entry_view is not None and entry_view.id() == view.id():
                entry.reanchor(view)


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

    # A blocking stat on the UI thread, which for a file on a sleeping or
    # unreachable network drive is not free. So it is reached only once every
    # in-memory route to the entry has failed, and only on a keypress. It is
    # what stops a deleted file being reopened as an empty buffer.
    if entry.file_name and Path(entry.file_name).is_file():
        # ENCODED_POSITION parses ":row:col" from the right, so Windows drive
        # letters ("C:\...") are safe. The entry re-attaches in on_load.
        window.open_file(f"{entry.file_name}:{entry.row + 1}:{entry.col + 1}", sublime.ENCODED_POSITION)
        return True

    history.remove(entry)
    return False


def _navigate(window: sublime.Window, step: Step) -> None:
    """Move ``step`` (-1 back, +1 forward) through the window's history.

    Every press goes somewhere visibly different, so targets near the current
    cursor are skipped (without being removed). That covers the newest entry
    when going back from the head, and entries that collapsed onto the same
    spot because the text between them was deleted. Targets that cannot be
    shown are dropped and skipped. Both continue in the same direction.
    ``history.index`` only moves when a target is actually shown. Reports the
    outcome in the status bar.
    """
    # No sweep for entries that can no longer be shown: that would cost an
    # is_valid round trip per attached entry on every keypress. _go already
    # drops an entry it cannot show, and navigation carries on in the same
    # direction, so a sweep only did the same work eagerly.
    history = _State.histories.get(window.id())
    if history is None or not history.entries:
        sublime.status_message("EditTrail: no edit locations recorded")
        return

    # Navigation does not move the cursor until a target is shown, so one
    # lookup serves every candidate, however many are skipped or dropped.
    cursor = _active_cursor(window)
    target = history.index + step
    while 0 <= target < len(history.entries):
        entry = history.entries[target]
        if _near_cursor(cursor, entry):
            target += step
            continue
        if _go(window, history, entry):
            history.index = target
            sublime.status_message(f"EditTrail: edit location {target + 1}/{len(history.entries)}")
            return
        # The unreachable entry was removed (and history.index adjusted if it
        # was older). Going back, the next older entry is at target - 1. Going
        # forward, the next newer one slid into target.
        if step < 0:
            target -= 1

    history.index = min(history.index, len(history.entries))
    sublime.status_message("EditTrail: no older edit locations" if step < 0 else "EditTrail: no newer edit locations")


@final
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
        if not _State.refresh_pending:
            # Debounced: one timer per burst of typing, not one per keystroke,
            # and no API call at all for the rest of the burst. Scheduled
            # before the guards below because a change this handler does not
            # record still moves the tracking regions of entries in this view.
            _State.refresh_pending = True
            sublime.set_timeout(_refresh_anchors, ANCHOR_REFRESH_DELAY_MS)
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
        try:
            # One round trip: indexing an empty Selection raises IndexError,
            # whereas len() first would be a second call.
            point = view.sel()[0].b
        except IndexError:
            return
        if _Config.debug:  # guarded so the message is not built on every keystroke
            _debug(f"recorded edit at line {view.rowcol(point)[0] + 1}", view)
        history = _State.histories.get(window.id())
        if history is None:
            history = _State.histories[window.id()] = History()
        history.record(view, point)

    def on_pre_close(self, view: sublime.View) -> None:
        """Detach entries from a closing view, or hand them to a clone."""
        view_id = view.id()
        _State.trackable.discard(view_id)
        # One lookup per window that turns out to have entries in this view,
        # because the answer differs per window (see _other_view_of_buffer).
        # Almost always just the one: the window the view is in.
        clones: dict[int, sublime.View | None] = {}
        for window_id, history in _State.histories.items():
            # Copied because an entry that detaches to nothing is removed below.
            for entry in list(history.entries):
                entry_view = entry.view
                if entry_view is None or entry_view.id() != view_id:
                    continue
                if window_id not in clones:
                    clones[window_id] = _other_view_of_buffer(view, window_id)
                clone = clones[window_id]
                if clone is None:
                    entry.detach()
                    if not entry.is_reachable():
                        # An unsaved buffer with no view left: nothing to
                        # reopen, ever. Dropped here, which costs no API calls,
                        # rather than by a sweep on the navigation keypress.
                        history.remove(entry)
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
        # Compared as Path objects rather than strings, because on Windows
        # Path equality folds case and separators, and Sublime can hand back a
        # path spelled differently from the one recorded (from a project file,
        # a symlink, or the goto-anything index). A string == would leave the
        # entry detached for good, reopening the file on every visit instead of
        # re-attaching. The target is built once, not per entry.
        target = Path(file_name)
        for entry in history.entries:
            if entry.view is None and entry.file_name and Path(entry.file_name) == target:
                entry.attach(view)

    def on_revert(self, view: sublime.View) -> None:
        """Re-anchor entries after File > Revert refilled the buffer."""
        _reanchor(view)

    def on_reload(self, view: sublime.View) -> None:
        """Re-anchor entries after the file was reloaded from disk."""
        _reanchor(view)

    def on_pre_close_window(self, window: sublime.Window) -> None:
        """Forget a closing window's history and its views' cached kinds.

        Sublime does not reliably send ``on_pre_close`` for every tab when a
        whole window goes, so the cached trackable flags are dropped here too.
        Otherwise ``_State.trackable`` would keep one entry per view for the
        rest of the session: small, but unbounded, which the memory note in the
        module docstring promises it is not.
        """
        for view in window.views():
            _State.trackable.discard(view.id())
        history = _State.histories.pop(window.id(), None)
        if history is not None:
            history.clear()


@final
class EditTrailBackCommand(sublime_plugin.WindowCommand):
    """Go to the previous (older) edit location."""

    def run(self, **_kwargs: object) -> None:
        """Run the command. Takes no arguments; ``**_kwargs`` matches the base signature."""
        _navigate(self.window, -1)

    def is_enabled(self, **_kwargs: object) -> bool:
        """Enable only when there is an older entry to go to.

        Sublime calls this whenever it draws the command palette or menus, so
        it is a dictionary lookup with no API calls and no side effects (no
        pruning). It can therefore be optimistic: an entry near the cursor or
        in a deleted file still counts, and running the command then reports
        that nothing older is left.
        """
        history = _State.histories.get(self.window.id())
        return history is not None and history.index > 0


@final
class EditTrailForwardCommand(sublime_plugin.WindowCommand):
    """Go to the next (newer) edit location."""

    def run(self, **_kwargs: object) -> None:
        """Run the command. Takes no arguments; ``**_kwargs`` matches the base signature."""
        _navigate(self.window, 1)

    def is_enabled(self, **_kwargs: object) -> bool:
        """Enable only after going back, when a newer entry exists.

        Same constraints as :meth:`EditTrailBackCommand.is_enabled`.
        """
        history = _State.histories.get(self.window.id())
        return history is not None and history.index + 1 < len(history.entries)


def _read_settings() -> None:
    """Copy the settings into _Config, falling back to defaults on bad values."""
    settings = sublime.load_settings(SETTINGS_FILE)
    max_entries = settings.get("max_entries", DEFAULT_MAX_ENTRIES)
    merge_distance = settings.get("merge_line_distance", DEFAULT_MERGE_LINE_DISTANCE)
    # bool is excluded explicitly because isinstance(True, int) is True, so
    # "max_entries": true would otherwise validate and cap the history at one.
    _Config.max_entries = (
        max_entries
        if isinstance(max_entries, int) and not isinstance(max_entries, bool) and max_entries > 0
        else DEFAULT_MAX_ENTRIES
    )
    _Config.merge_line_distance = (
        merge_distance
        if isinstance(merge_distance, int) and not isinstance(merge_distance, bool) and merge_distance >= 0
        else DEFAULT_MERGE_LINE_DISTANCE
    )
    _Config.debug = settings.get("debug", False) is True
    # Apply a lowered max_entries now, not at the next edit.
    for history in _State.histories.values():
        history.trim()


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
    # A refresh already scheduled still fires, harmlessly, over empty histories.
    _State.refresh_pending = False
    sublime.load_settings(SETTINGS_FILE).clear_on_change("edit_trail")
