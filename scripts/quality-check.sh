#!/usr/bin/env bash
# Pre-commit quality gate for grok-build-auth.
# Runs ruff format + ruff lint (and pyright if available) over STAGED python files.
# Fails the commit on any formatting drift or lint error.
#
# Enable once per clone:  git config core.hooksPath githooks
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

# Collect staged *.py files (Added/Copied/Modified/Renamed), NUL-safe.
# `while read` keeps this compatible with macOS bash 3.2 (no `mapfile`).
files=()
while IFS= read -r -d '' f; do
    # Keep only paths that still exist (skip deletions racing the index).
    [ -f "$f" ] && files+=("$f")
done < <(git diff --cached --name-only --diff-filter=ACMR -z -- '*.py')

if [ "${#files[@]}" -eq 0 ]; then
    exit 0
fi

# Prefer a project-local ruff, else uvx (no global install required).
if command -v ruff >/dev/null 2>&1; then
    ruff=(ruff)
elif command -v uvx >/dev/null 2>&1; then
    ruff=(uvx ruff)
else
    echo "pre-commit: neither 'ruff' nor 'uvx' found on PATH — install one to commit." >&2
    exit 1
fi

echo "pre-commit: ruff format (${#files[@]} staged file(s))"
if ! "${ruff[@]}" format --check "${files[@]}"; then
    echo "" >&2
    echo "pre-commit: formatting drift. Run:  ${ruff[*]} format ${files[*]}" >&2
    echo "            then 'git add' the changes and re-commit." >&2
    exit 1
fi

echo "pre-commit: ruff check"
if ! "${ruff[@]}" check "${files[@]}"; then
    echo "" >&2
    echo "pre-commit: lint errors above. Fix, or auto-fix:  ${ruff[*]} check --fix ${files[*]}" >&2
    exit 1
fi

# pyright is advisory: run only when installed, and never block on pre-existing
# third-party stub gaps. Non-fatal by design.
if command -v pyright >/dev/null 2>&1; then
    echo "pre-commit: pyright (advisory)"
    pyright "${files[@]}" || echo "pre-commit: pyright reported issues (advisory, not blocking)." >&2
fi

echo "pre-commit: OK"
