"""Checks on how edit_trail plugs into Sublime, which the behaviour tests cannot make.

The behaviour tests call the listener's hooks directly, so a hook whose name Sublime never
dispatches passes every one of them and then silently never fires in the editor. Nothing else
catches that either: ``sublime_plugin.EventListener`` declares no hook methods at all (the dispatch
side is module-level functions keyed by name, ``sublime_plugin.all_callbacks``), so an overridden
hook is not an override as far as a type checker is concerned, and a typo is just a new method.

``sublime_plugin`` cannot be imported outside the editor, so the names it dispatches are copied
here as a literal. Regenerate with, against a Sublime install::

    python -c "import re,sys; src=open(sys.argv[1],encoding='utf8').read();
    blk=src[src.index('all_callbacks = {'):]; print(sorted(set(re.findall(r\"'(on_[a-z_0-9]+)'\",
    blk[:blk.index(chr(10)+'}')]))))" "<Sublime>/Lib/python314/sublime_plugin.py"

Behaviour the API promises at runtime, rather than names, is checked by ``st_tests/``, which runs
inside the editor.
"""

from __future__ import annotations

import ast
from pathlib import Path

import edit_trail

# sublime_plugin.all_callbacks, Sublime Text build 4213. Names an EventListener may define; anything
# else starting with "on_" on a listener class is dead code.
EVENT_LISTENER_CALLBACKS = frozenset(
    {
        "on_activated",
        "on_activated_async",
        "on_associate_buffer",
        "on_associate_buffer_async",
        "on_clone",
        "on_clone_async",
        "on_close",
        "on_close_buffer",
        "on_close_buffer_async",
        "on_deactivated",
        "on_deactivated_async",
        "on_exit",
        "on_hover",
        "on_init",
        "on_load",
        "on_load_async",
        "on_load_project",
        "on_load_project_async",
        "on_modified",
        "on_modified_async",
        "on_new",
        "on_new_async",
        "on_new_buffer",
        "on_new_buffer_async",
        "on_new_project",
        "on_new_project_async",
        "on_new_window",
        "on_new_window_async",
        "on_post_move",
        "on_post_move_async",
        "on_post_save",
        "on_post_save_async",
        "on_post_save_project",
        "on_post_save_project_async",
        "on_post_text_command",
        "on_post_window_command",
        "on_pre_close",
        "on_pre_close_project",
        "on_pre_close_window",
        "on_pre_move",
        "on_pre_save",
        "on_pre_save_async",
        "on_pre_save_project",
        "on_query_completions",
        "on_query_context",
        "on_reload",
        "on_reload_async",
        "on_revert",
        "on_revert_async",
        "on_selection_modified",
        "on_selection_modified_async",
        "on_text_command",
        "on_window_command",
    }
)

# Module-level functions Sublime looks up by name when it loads or unloads the plugin
# (sublime_plugin.load_module / unload_module).
MODULE_HOOKS = frozenset({"plugin_loaded", "plugin_unloaded"})

_SOURCE = ast.parse(Path(edit_trail.__file__).read_text(encoding="utf-8"))


def _base_names(node: ast.ClassDef) -> set[str]:
    """Attribute names of a class's bases, e.g. {"EventListener"} for sublime_plugin.EventListener."""
    return {base.attr for base in node.bases if isinstance(base, ast.Attribute)}


def _methods(node: ast.ClassDef) -> list[str]:
    return [child.name for child in node.body if isinstance(child, ast.FunctionDef)]


def _listener_classes() -> list[ast.ClassDef]:
    return [
        node
        for node in ast.walk(_SOURCE)
        if isinstance(node, ast.ClassDef) and _base_names(node) & {"EventListener", "ViewEventListener"}
    ]


def test_the_plugin_defines_a_listener() -> None:
    """Guard the tests below: they pass vacuously if the listener class stops being found."""
    assert [node.name for node in _listener_classes()] == ["EditTrailListener"]


def test_every_listener_hook_is_a_name_sublime_dispatches() -> None:
    """A hook Sublime does not know about is never called, however well it is tested here."""
    for node in _listener_classes():
        hooks = {name for name in _methods(node) if name.startswith("on_")}
        assert hooks <= EVENT_LISTENER_CALLBACKS, f"{node.name}: {sorted(hooks - EVENT_LISTENER_CALLBACKS)}"


def test_listener_hooks_are_public_methods_of_the_listener() -> None:
    """Every hook the tests drive is on the listener class, not a loose function Sublime cannot see."""
    loose = {node.name for node in _SOURCE.body if isinstance(node, ast.FunctionDef) and node.name.startswith("on_")}
    assert not loose, f"module-level {sorted(loose)}: Sublime dispatches hooks on listener classes only"


def test_module_hooks_are_named_as_sublime_looks_them_up() -> None:
    """plugin_loaded / plugin_unloaded are found by name; a typo leaves settings unread on load."""
    defined = {
        node.name for node in _SOURCE.body if isinstance(node, ast.FunctionDef) and node.name.startswith("plugin_")
    }
    assert defined == MODULE_HOOKS
    for name in MODULE_HOOKS:
        assert callable(getattr(edit_trail, name))
