# Developing EditTrail

A development file: `.gitattributes` keeps it out of the archive Package Control installs from.

## Sublime Text, for a Python developer who has never written a plugin

The code assumes you know Python well and the Sublime API not at all. This is what the module
docstring takes for granted. Full reference: <https://www.sublimetext.com/docs/api_reference.html>

**How a package becomes running code.** A package is a directory under Sublime's `Packages/`.
Sublime imports the top-level `.py` files of each package as plugins, all into one shared
`plugin_host` process; subdirectories are never imported. That is why `tests/` sitting inside the
installed package is harmless, and why `edit_trail_selftest.py` has to be at the top level.
`plugin_loaded()` and `plugin_unloaded()` are module-level functions found by name. Commands are
classes: `SomethingElseCommand` is invoked as `run_command("something_else")`, where a
`WindowCommand` gets `self.window` and a `TextCommand` gets `self.view`.

**Event listeners are dispatched by name, not by override.** `sublime_plugin.EventListener` declares
no hook methods at all; Sublime looks them up by name in a table. A misspelled `on_modfied` is not a
failed override, it is just a new method that never runs, and no type checker can see the problem.
That is what `tests/test_api_contract.py` exists to catch.

**The object model:**

| Term | What it is |
| --- | --- |
| Buffer | The text, with an id of its own |
| View | A window onto a buffer, with its own cursor, scroll position and regions. A split pane or `File > New View into File` is a second view of one buffer, so `view.buffer_id()` is what tells a clone from a genuinely different file |
| Region | A span `(a, b)` into the text. Zero width means a caret position |
| Selection | `view.sel()`, the view's carets. A live handle onto the view: `clear()` on it really does leave the view with no cursor |
| Window | Holds views. `Window.views()` omits the transient (preview) tab unless you ask for it |

**The two API facts the whole plugin rests on.** First, `view.add_regions(key, regions, flags)`
stores named regions and *Sublime moves them as the text changes*. That is the entire tracking
mechanism: a remembered location is a hidden zero-width region, and an edit above it pushes it down
for free. `get_regions(key)` reads them back, `erase_regions(key)` drops them. Second, text can only
be changed inside a `TextCommand.run(self, edit)` — the `edit` token cannot be created or stored,
which is the only reason `edit_trail_selftest.py` exists.

**Not every view is a file.** Panels, the console, the find input and build output are views too,
and they fire `on_modified` on every keystroke. `view.element()`, the `is_widget` setting and
`view.is_scratch()` are how they are told apart.

**`sublime.set_timeout(fn, ms)` runs `fn` on the UI thread** and is the only scheduler there is. The
fake queues them instead of running them, so a test decides when idle work happens.

**Almost everything costs a round trip.** Most methods on `View` and `Window` are calls into the
editor core (`sublime_api`), not attribute access: `view.id()` is cheap Python, `view.size()` is
not. `View.sel()` is the odd one out — it hands back a `Selection` bound to the view without calling
in, and only indexing that costs. That asymmetry is why the Performance section counts what it
counts.

**The API is not on PyPI.** `sublime.py` and `sublime_plugin.py` live in Sublime's own
`Lib/python314`, fully annotated, which is why the type checker is pointed at an installed copy.

## Read this first

The design is not in this file. It is the module docstring at the top of `edit_trail.py`, and it is
the thing to read before changing anything:

| Section | Answers |
| --- | --- |
| How recording works | When an edit becomes a location, and when it merges into the newest one |
| How positions stay correct | Tracking regions, the fallback row/column, and why both exist |
| Windows | Why history is per window, and what a tab dragged between windows does |
| Navigation semantics | What back and forward skip, drop and report |
| Performance | The API round trips `on_modified` is allowed, counted |
| Pitfalls this avoids | Decisions that look wrong until you know what they prevent |
| API contract relied on | The Sublime behaviours the whole design rests on |
| Compatibility | The Python 3.8 floor and the build each API needs |

Classes carry their own docstrings in the same register. `Entry`, `History` and `_State` are worth
reading in that order.

## Repository map

| Path | What it is |
| --- | --- |
| `edit_trail.py` | The whole plugin. One module, because Sublime loads top-level files only |
| `Default.sublime-commands`, `Default.sublime-keymap`, `Main.sublime-menu` | Palette entries, example bindings, menu |
| `EditTrail.sublime-settings` | Defaults and their documentation, as users see them |
| `messages.json`, `messages/install.txt` | What Package Control shows on install |
| `tests/` | Unit tests against a fake Sublime API. No editor needed |
| `st_tests/` | Tests that need the real editor, run by UnitTesting |
| `unittesting.json` | Points UnitTesting at `st_tests`. Without it, it would load `tests/` and inject the fake `sublime` module into your running editor |
| `edit_trail_selftest.py` | The one `TextCommand` `st_tests` needs, and nothing else. A TextCommand is the only way to get an `Edit` token, and it has to live in a top-level file |
| `justfile` | Every task. The comments explain the setup; read them rather than the recipes |
| `EditTrail.sublime-project` | Dev project: folder excludes and debugger configurations |
| `.gitattributes` | `export-ignore` is what keeps development files out of releases |

## Setup

Needs [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/). Nothing else is installed
globally: `uv run` syncs the dev environment from `pyproject.toml` and `uv.lock`.

```sh
just install-dev   # link this checkout into Sublime's Packages dir (junction on Windows)
just qa            # lint, format check, type check, tests on 3.14 and on 3.8
```

Three things surprise people:

- **`.python-version` says 3.8 and that is deliberate.** Sublime reads it to pick its modern plugin
  host. uv reads the same file and would try to build a 3.8 dev environment, which
  `requires-python` rejects, so the justfile exports `UV_PYTHON=3.14` for every uv call. Running
  `uv run` yourself outside `just` needs the same variable set.
- **`just typecheck` needs Sublime installed locally.** `sublime.py` and `sublime_plugin.py` are not
  on PyPI, so ty is pointed at Sublime's own `Lib/python314`. Override the location with
  `SUBLIME_API_DIR`, and the Packages directory with `SUBLIME_PACKAGES`, for portable installs.
- **`just install-dev` links, it does not copy.** Saving `edit_trail.py` reloads the plugin in the
  running editor. `just uninstall-dev` clears the slot; it unlinks a link and only deletes a real
  directory.

## The four test layers

Each catches something the others cannot. Adding a behaviour usually means touching the first two.

| Layer | Runs | Catches | Blind to |
| --- | --- | --- | --- |
| `tests/test_edit_trail.py` | `just test` | A named situation behaving wrong. These are the documentation of intent | Orders nobody wrote down |
| `tests/test_properties.py` | `just test` | Invariants broken by a command order nobody thought of, shrunk to a minimal sequence | Anything the fake models wrongly |
| `tests/test_api_contract.py` | `just test` | A listener hook Sublime never dispatches — a typo'd or invented `on_*` name is not an override and nothing else notices | Runtime behaviour |
| `st_tests/` | UnitTesting, in a real editor | A Sublime release changing a behaviour the design rests on | Everything about our own logic |

The fake lives in `tests/conftest.py` and its docstring states every behaviour it models. When you
teach it something new, add the matching check to `st_tests/` — otherwise the fake becomes a place
where assumptions go unverified.

The property tests use [Hypothesis](https://hypothesis.readthedocs.io/en/latest/stateful.html),
which needs a newer Python than the 3.8 floor, so the 3.8 run skips that module. When you add a rule
or an invariant there, check it has teeth: break the code it is meant to guard, confirm the run goes
red, then put the code back.

## Running the in-editor tests

In CI, `.github/workflows/tests.yml` runs them on Linux, macOS and Windows against a real Sublime
build on every push. `SublimeText/UnitTesting/actions/setup@v1` installs Sublime, starts Xvfb, copies
this checkout into the Packages directory and installs Package Control; `run-tests@v1` launches the
editor, and the results come back through a log file it tails.

Locally: install the UnitTesting package, `just install-dev`, then run
**UnitTesting: Test Current Package** from the command palette.

**When that job goes red and `just qa` is green, the editor changed, not the plugin.** Find the
assumption that failed, check the matching bullet under "API contract relied on" in
`edit_trail.py`, and decide whether the design still holds. Do not "fix" the test.

## Debugging

- `debug: true` in the settings prints each edit decision to the console (View > Show Console),
  saying whether it recorded a location, merged into the newest one, or ignored the change.
- `just debug-test <pattern>` drops into pdb at the first line of every matching test. No Sublime
  packages needed; run it in a terminal such as Terminus.
- For a graphical debugger, open `EditTrail.sublime-project` and use its debugger configurations.
  Its header comment lists the keys.

## Conventions

- **Names declare the domain thing and its kind.** A plural noun means a collection, so a quantity
  takes `_COUNT`; a bool reads as a claim (`PATHS_ARE_CASE_INSENSITIVE`); string content takes
  `_TEXT`; a collection of identifiers says so (`trackable_view_ids`). This holds in tests and
  fixtures exactly as much as in `edit_trail.py`.
- **Comments say why, not what.** The existing density is the standard. A decision that looks odd
  gets the reason that makes it obvious.
- **The round-trip accounting in the Performance section is a contract, not a note.** `on_modified`
  runs on every keystroke. If you change what it calls, recount and update the numbers.
- **The plugin has no dependencies and gets none.** It ships as one file to editors we do not
  control. Development tooling lives in the dev dependency group.
- **Python 3.8 syntax, because a stable Sublime may still run that host.** `from __future__ import
  annotations` makes modern annotations safe; runtime constructs are not covered by it, which is
  what the 3.8 test run is there to catch.
- Run `just qa` before pushing. It is what CI runs, minus the type check.

## Releasing

Package Control installs from a GitHub archive of a tag, and `git archive` honours `export-ignore`
the same way, so `just package-files` shows exactly what users would receive. It reads committed
files only, so commit first. Anything new and development-only needs its `export-ignore` line in
`.gitattributes`, and `just package-files` is how you confirm it.

`messages/install.txt` is shown once on install. Keep it in step with the README's Usage section.
