# EditTrail

Go back and forward through the places you edited, across files, in Sublime Text 4. Like VS Code's
"Go to Last Edit Location".

Sublime's `jump_back` follows every cursor jump, and `prev_modification` stays within one file. EditTrail
follows only your edits, in the order you made them.

## Usage

No keys are bound by default. Add your own via **Preferences: EditTrail Key Bindings**, for example:

```json
{ "keys": ["ctrl+."], "command": "edit_trail_back" },
{ "keys": ["ctrl+shift+."], "command": "edit_trail_forward" },
```

Both commands are also in the command palette under "EditTrail".

Nearby edits merge into one location, locations move with the text, and closed files reopen when you go
back to them. History is per window and resets when Sublime restarts.

## Settings

**Preferences: EditTrail Settings**

- `max_entries` (50): edit locations kept per window.
- `merge_line_distance` (5): edits this close to the newest location, in the same file, update it.
  The same distance is what counts as "already here" when navigating, so locations within it of the
  cursor are skipped.
- `debug` (false): print each edit to the console (View > Show Console), saying whether it added a
  location, merged into the newest one, or was ignored. Useful when a location shows up that you did
  not expect.

## Development

Needs [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/).

```sh
just install-dev   # link this checkout into Sublime's Packages dir
just qa            # lint, format check, type check, tests on Python 3.14 and 3.8
```

Run `just` to see all recipes. The justfile comments explain the setup, including why `UV_PYTHON` is
exported.
