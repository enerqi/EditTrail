"""Fake Sublime Text API for unit testing edit_trail outside the editor.

Only the parts of ``sublime`` / ``sublime_plugin`` that edit_trail touches are modelled, with just enough
behaviour to exercise its logic:

* A FakeBuffer holds text shared by every FakeView cloned from it.
* Regions are stored per view as single points and shift on insertion, mimicking how Sublime moves
  regions when text is inserted before them.
* FakeSelection is backed by the view, as Sublime's Selection is, so clear() really does leave the view
  with no cursor and an sel()[0] on it raises IndexError. Code that focuses a view without placing a
  cursor therefore fails a test instead of passing by accident.
* FakeView.type() inserts text, moves the cursor, focuses the view, marks it dirty and fires the listener's
  on_modified, standing in for a keystroke. FakeView.external_change() fires on_modified the way a load,
  restore or reload does: text changed, nothing unsaved. FakeView.reload_from_disk() / revert() refill the
  buffer and fire on_reload / on_revert. FakeView.close() fires on_pre_close first, as Sublime does.
* FakeWindow.open_file() only records what was requested; tests create the reopened view and call
  on_load themselves, which is how Sublime sequences a real reopen.
* sublime.set_timeout() queues the callback instead of running it. run_timeouts() drains the queue,
  standing in for the editor going idle, so a test decides when the debounced anchor refresh happens.

The fake modules are registered in sys.modules at import time, before any test imports edit_trail.
"""

from __future__ import annotations

import sys
import types
from itertools import count
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_ids: Iterator[int] = count(1)


class FakeRegion:
    def __init__(self, a: int, b: int | None = None) -> None:
        self.a = a
        self.b = a if b is None else b


class FakeSettings:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = dict(values or {})
        self.callbacks: dict[str, Callable[[], None]] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value
        for callback in list(self.callbacks.values()):
            callback()

    def add_on_change(self, tag: str, callback: Callable[[], None]) -> None:
        self.callbacks[tag] = callback

    def clear_on_change(self, tag: str) -> None:
        self.callbacks.pop(tag, None)


class FakeBuffer:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self._views: list[FakeView] = []

    def views(self) -> list[FakeView]:
        return [v for v in self._views if v.valid]


class FakeSelection:
    """Backed by the view, like Sublime's: clear() and add() mutate the view itself."""

    def __init__(self, view: FakeView) -> None:
        self._view = view

    def __len__(self) -> int:
        return 0 if self._view.cursor is None else 1

    def __getitem__(self, index: int) -> FakeRegion:
        if self._view.cursor is None:
            raise IndexError(index)
        return FakeRegion(self._view.cursor)

    def clear(self) -> None:
        self._view.cursor = None

    def add(self, region: FakeRegion) -> None:
        self._view.cursor = region.b


class FakeView:
    def __init__(
        self,
        window: FakeWindow,
        file_name: str | None = None,
        text: str = "",
        buffer: FakeBuffer | None = None,
        *,
        scratch: bool = False,
        widget: bool = False,
        element: str | None = None,
    ) -> None:
        self._id = next(_ids)
        self.win = window
        self.fname = file_name
        self.buf = buffer if buffer is not None else FakeBuffer(text)
        self.buf._views.append(self)
        self.regions: dict[str, int] = {}
        self._settings = FakeSettings({"is_widget": True} if widget else {})
        self.cursor: int | None = 0
        self.valid = True
        self.scratch = scratch
        self._element = element
        self.dirty = False
        self.loading = False
        window._views.append(self)

    # Sublime API surface
    def id(self) -> int:
        return self._id

    def is_valid(self) -> bool:
        return self.valid

    def is_loading(self) -> bool:
        return self.loading

    def is_dirty(self) -> bool:
        return self.dirty

    def name(self) -> str:
        return ""

    def is_scratch(self) -> bool:
        return self.scratch

    def element(self) -> str | None:
        return self._element

    def window(self) -> FakeWindow | None:
        return self.win if self.valid else None

    def file_name(self) -> str | None:
        return self.fname

    def buffer(self) -> FakeBuffer:
        return self.buf

    def settings(self) -> FakeSettings:
        return self._settings

    def size(self) -> int:
        return len(self.buf.text)

    def sel(self) -> FakeSelection:
        return FakeSelection(self)

    def rowcol(self, point: int) -> tuple[int, int]:
        before = self.buf.text[:point]
        return before.count("\n"), point - (before.rfind("\n") + 1)

    def text_point(self, row: int, col: int) -> int:
        lines = self.buf.text.split("\n")
        row = min(row, len(lines) - 1)
        return sum(len(line) + 1 for line in lines[:row]) + col

    def line(self, point: int) -> FakeRegion:
        text = self.buf.text
        start = text.rfind("\n", 0, point) + 1
        end = text.find("\n", point)
        return FakeRegion(start, len(text) if end < 0 else end)

    def add_regions(self, key: str, regions: list[FakeRegion], **_kwargs: Any) -> None:
        self.regions[key] = regions[0].b

    def get_regions(self, key: str) -> list[FakeRegion]:
        return [FakeRegion(self.regions[key])] if key in self.regions else []

    def erase_regions(self, key: str) -> None:
        self.regions.pop(key, None)

    def show_at_center(self, point: int) -> None:
        pass

    # Test helpers
    def _insert(self, point: int, text: str) -> None:
        self.buf.text = self.buf.text[:point] + text + self.buf.text[point:]
        for view in self.buf.views():
            for key, region_point in view.regions.items():
                if region_point >= point:
                    view.regions[key] = region_point + len(text)

    def type(self, point: int, text: str) -> None:
        """Insert text at point as a user keystroke would (focused, dirty), then fire on_modified."""
        self._insert(point, text)
        self.cursor = point + len(text)
        self.dirty = True
        self.win.active = self
        _listener().on_modified(self)

    def external_change(self, point: int, text: str, *, loading: bool = False) -> None:
        """Change text the way a load, restore or reload does: no unsaved edits, cursor untouched."""
        self._insert(point, text)
        self.dirty = False
        self.loading = loading
        _listener().on_modified(self)
        self.loading = False

    def reload_from_disk(self, text: str, *, keep_regions: bool = True) -> None:
        """Refill the buffer from disk, as a reload of a file changed outside Sublime does.

        ``keep_regions=False`` models Sublime dropping the tracking regions when it
        replaces the whole buffer.
        """
        self.buf.text = text
        self.dirty = False
        if not keep_regions:
            self.regions.clear()
        _listener().on_reload(self)

    def revert(self, text: str, *, keep_regions: bool = True) -> None:
        """Refill the buffer as File > Revert does."""
        self.buf.text = text
        self.dirty = False
        if not keep_regions:
            self.regions.clear()
        _listener().on_revert(self)

    def type_at_row(self, row: int, text: str) -> None:
        self.type(self.text_point(row, 0), text)

    def close(self) -> None:
        _listener().on_pre_close(self)
        self.valid = False
        self.win._views.remove(self)

    def cursor_row(self) -> int:
        return self.rowcol(self.cursor)[0]


class FakeWindow:
    def __init__(self) -> None:
        self._id = next(_ids)
        self._views: list[FakeView] = []
        self.active: FakeView | None = None
        self.opened: list[str] = []

    def id(self) -> int:
        return self._id

    def views(self) -> list[FakeView]:
        return list(self._views)

    def active_view(self) -> FakeView | None:
        return self.active

    def focus_view(self, view: FakeView) -> None:
        self.active = view

    def bring_to_front(self) -> None:
        pass

    def find_open_file(self, file_name: str) -> FakeView | None:
        return next((v for v in self._views if v.fname == file_name), None)

    def open_file(self, spec: str, flags: int = 0) -> None:
        self.opened.append(spec)


class _EventListener:
    pass


class _WindowCommand:
    def __init__(self, window: FakeWindow) -> None:
        self.window = window


status_messages: list[str] = []
plugin_settings: FakeSettings = FakeSettings()
timeouts: list[Callable[[], None]] = []


def run_timeouts() -> None:
    """Run everything queued with sublime.set_timeout, as the editor does when it goes idle."""
    while timeouts:
        timeouts.pop(0)()


_sublime = types.ModuleType("sublime")
_sublime.Region = FakeRegion  # ty: ignore[unresolved-attribute]
_sublime.HIDDEN = 256  # ty: ignore[unresolved-attribute]
_sublime.ENCODED_POSITION = 1  # ty: ignore[unresolved-attribute]
_sublime.status_message = status_messages.append  # ty: ignore[unresolved-attribute]
_sublime.load_settings = lambda _name: plugin_settings  # ty: ignore[unresolved-attribute]
_sublime.set_timeout = lambda callback, _delay=0: timeouts.append(callback)  # ty: ignore[unresolved-attribute]
_sublime_plugin = types.ModuleType("sublime_plugin")
_sublime_plugin.EventListener = _EventListener  # ty: ignore[unresolved-attribute]
_sublime_plugin.WindowCommand = _WindowCommand  # ty: ignore[unresolved-attribute]
sys.modules["sublime"] = _sublime
sys.modules["sublime_plugin"] = _sublime_plugin

import edit_trail  # noqa: E402 - must follow the fake module registration above

_LISTENER: edit_trail.EditTrailListener = edit_trail.EditTrailListener()


def _listener() -> edit_trail.EditTrailListener:
    return _LISTENER


@pytest.fixture(autouse=True)
def fresh_plugin() -> Iterator[None]:
    """Give every test a freshly loaded plugin with default settings."""
    plugin_settings.values.clear()
    plugin_settings.callbacks.clear()
    status_messages.clear()
    timeouts.clear()
    edit_trail.plugin_loaded()
    yield
    edit_trail.plugin_unloaded()


@pytest.fixture
def listener() -> edit_trail.EditTrailListener:
    return _LISTENER


@pytest.fixture
def window() -> FakeWindow:
    return FakeWindow()


BODY = "\n".join(f"line{i}" for i in range(100))
