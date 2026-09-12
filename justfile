# task runner
#
# SHELL: cmd.exe on Windows, bash elsewhere, for one-liners. Anything needing a conditional, a loop or a
# platform check is a `[script]` recipe run with `script-interpreter` (stdlib-only Python via uv).
#
# INTERPRETER: uv resolves the Python for scripts on every platform. `--no-project` keeps script recipes
# out of pyproject.toml / .venv, so they never trigger a sync.
#
# DOC COMMENTS: `just --list` shows only the LAST comment line above a recipe. Longer explanations go
# above a `# ---` separator, with the one-line summary below it.
[windows]
set shell := ["cmd.exe", "/c"]
[unix]
set shell := ["bash", "-c"]
set script-interpreter := ["uv", "run", "--no-project", "-p", "3.14", "python"]
set unstable                        # [script] + script-interpreter
set minimum-version := "1.33.0"     # [script] (1.33), [group] (1.27)

# .python-version holds "3.8" for Sublime Text (it selects the modern plugin host). uv reads the same file
# and would try to build a 3.8 dev environment, which pyproject's requires-python rejects. Exporting
# UV_PYTHON overrides the file for every uv call made from this justfile. Plain `uv run` outside just
# needs the same variable set (see README).
export UV_PYTHON := env("UV_PYTHON", "3.14")

# Sublime's per-user Packages directory. Override with SUBLIME_PACKAGES for portable installs.
sublime_packages := env("SUBLIME_PACKAGES", if os() == "windows" {
    env("APPDATA", "") / "Sublime Text" / "Packages"
} else if os() == "macos" {
    home_directory() / "Library" / "Application Support" / "Sublime Text" / "Packages"
} else {
    config_directory() / "sublime-text" / "Packages"
})

# Directory holding Sublime's own sublime.py / sublime_plugin.py, which ty needs to resolve the API.
# They are not published on PyPI. Override with SUBLIME_API_DIR for other install locations.
sublime_api_dir := env("SUBLIME_API_DIR", if os() == "windows" {
    "C:/Program Files/Sublime Text/Lib/python314"
} else if os() == "macos" {
    "/Applications/Sublime Text.app/Contents/MacOS/Lib/python314"
} else {
    "/opt/sublime_text/Lib/python314"
})

package_slot := sublime_packages / "EditTrail"

# List recipes.
default:
    @just --list

# Code quality (ruff + ty + pytest from the dev group). `uv run` syncs the project env first, so the tools
# and their pins come from pyproject.toml / uv.lock, with no global installs.

# Lint (ruff) + import sorting. Pass `args` e.g. `just lint --fix` to apply fixes.
[group('qa')]
lint *args:
    uv run ruff check . {{args}}

# Format with ruff. Pass `--check` to verify without writing.
[group('qa')]
format *args:
    uv run ruff format . {{args}}

# Type-check with ty against Sublime's installed API modules.
[group('qa')]
typecheck:
    uv run ty check --extra-search-path "{{sublime_api_dir}}"

# Run the unit tests (fake Sublime API, no editor needed). Pass pytest args e.g. `just test -k clone`.
[group('qa')]
test *args:
    uv run pytest {{args}}

# Stable Sublime builds (before 4205) run the plugin on a real Python 3.8 host, so the suite also runs on
# 3.8 to catch runtime incompatibilities that ruff/ty's 3.8 target cannot (stdlib behaviour, annotations
# evaluated at runtime). `--no-project` keeps this out of the 3.14 dev .venv; pytest 8.3 is the last line
# supporting 3.8. pytest 8 does not read pyproject's native `[tool.pytest]` table (a pytest 9 feature),
# hence the explicit pythonpath and test path.
# ---
# Run the unit tests on Python 3.8, the plugin's floor.
[group('qa')]
test-py38 *args:
    uv run --no-project -p 3.8 --with "pytest>=8.3,<8.4" python -m pytest -p no:cacheprovider -o pythonpath=. tests {{args}}

# Zero-setup debugging, no Sublime Debugger package needed: `--trace` drops into pdb at the first line of
# every test matching `pattern` (n = next, s = step into, c = continue, q = quit). Run it in a terminal
# such as Terminus. For the graphical debugger instead, open EditTrail.sublime-project and use its
# debugger_configurations.
# ---
# Debug tests matching a pattern in pdb, e.g. `just debug-test collapsed`.
[group('qa')]
debug-test pattern:
    uv run pytest -k "{{pattern}}" --trace -p no:cacheprovider

# Full local gate: lint + format check + typecheck + tests on the dev Python and on 3.8.
[group('qa')]
qa: lint (format "--check") typecheck test test-py38

# Two kinds of install land in the same Packages/EditTrail slot and conflict:
#   - dev link (just install-dev): unlinked, source checkout left intact
#   - a real directory (e.g. a manual copy): deleted recursively
# A Package Control install is a zip in "Installed Packages" instead; remove that through Package Control.
# os.path.islink covers posix symlinks and Windows symlinks but NOT junctions, which only show as a
# reparse-point attribute, and os.rmdir unlinks a junction without touching its target. That check is
# what stops shutil.rmtree recursing through the link into this checkout.
# ---
# Clear the Packages/EditTrail slot, whatever is there.
[group('sublime')]
[script]
uninstall-dev:
    import os, shutil, stat

    p = r"{{package_slot}}"
    if not os.path.lexists(p):
        print("Nothing installed at " + p)
        raise SystemExit(0)

    if os.path.islink(p):
        os.unlink(p)
        print("Removed dev symlink: " + p)
    elif os.name == "nt" and os.lstat(p).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        os.rmdir(p)  # unlinks the junction only; does NOT touch the source checkout
        print("Removed dev junction: " + p)
    else:
        shutil.rmtree(p)
        print("Removed directory: " + p)

# Sublime only loads packages from its Packages directory, so link this checkout in. Saving edit_trail.py
# then reloads the plugin immediately. Windows gets a junction via `mklink /J` (no admin rights needed,
# unlike a directory symlink, and the stdlib has no CreateJunction); elsewhere an ordinary symlink.
# ---
# Live-edit dev install: link this checkout into Sublime's Packages directory as EditTrail.
[group('sublime')]
[script]
install-dev: uninstall-dev
    import os, subprocess, sys

    src = r"{{justfile_directory()}}"
    dest = r"{{package_slot}}"
    if not os.path.isdir(os.path.dirname(dest)):
        sys.exit("Sublime Packages directory not found: " + os.path.dirname(dest) + " (set SUBLIME_PACKAGES)")

    if os.name == "nt":
        # mklink is a cmd builtin, not an exe, so it has to go through cmd /c.
        code = subprocess.run(["cmd", "/c", "mklink", "/J", dest, src], stdout=subprocess.DEVNULL).returncode
        if code:
            sys.exit(code)
        print("Junction created: " + dest + " -> " + src)
    else:
        os.symlink(src, dest)
        print("Symlink created: " + dest + " -> " + src)

# Package Control installs from a GitHub archive of a tag, and `git archive` honours export-ignore in
# .gitattributes the same way, so this is what users would receive. Needs at least one commit; it lists
# committed files only, so commit before trusting the output.
# ---
# List the files a release built from HEAD would ship.
[group('release')]
[script]
package-files ref="HEAD":
    import io, subprocess, sys, tarfile

    result = subprocess.run(["git", "archive", "--format=tar", "{{ref}}"], capture_output=True)
    if result.returncode:
        sys.exit(result.stderr.decode(errors="replace").strip())
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                print(member.name)
