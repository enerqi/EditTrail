"""Dev-only: the text-editing command the in-editor API tests drive.

The checks themselves live in ``st_tests/``, where UnitTesting discovers and runs them (see
``unittesting.json``). They cannot change a buffer from where they sit: a ``TextCommand`` is the only
way to get an ``Edit`` token, and a TextCommand has to be in a module Sublime loads as a plugin,
which means a file at the top of the package. So this is that file, and nothing else.

``.gitattributes`` keeps it, ``st_tests/`` and ``unittesting.json`` out of the archive Package
Control installs from, so users never receive any of them.
"""

from __future__ import annotations

import sublime
import sublime_plugin


class EditTrailSelftestEditCommand(sublime_plugin.TextCommand):
    """Apply one text change for the API tests: insert, erase, or replace the whole buffer."""

    def run(self, edit: sublime.Edit, **kwargs: sublime.Value) -> None:
        """Insert ``text`` at ``point``, erase ``erase_from``-``erase_to``, or replace the whole buffer.

        Arguments arrive as a dict because Sublime passes command arguments as keywords typed
        ``Value``; they are narrowed here rather than annotated in the signature, which must stay
        compatible with ``TextCommand.run``.
        """
        operation = str(kwargs.get("operation", ""))
        text = str(kwargs.get("text", ""))
        point = int(kwargs.get("point", 0))  # ty: ignore[invalid-argument-type]
        erase_from = int(kwargs.get("erase_from", 0))  # ty: ignore[invalid-argument-type]
        erase_to = int(kwargs.get("erase_to", 0))  # ty: ignore[invalid-argument-type]
        if operation == "insert":
            self.view.insert(edit, point, text)
        elif operation == "erase":
            self.view.erase(edit, sublime.Region(erase_from, erase_to))
        elif operation == "replace_all":
            self.view.replace(edit, sublime.Region(0, self.view.size()), text)
        else:
            message = f"unknown edit operation: {operation}"
            raise ValueError(message)
