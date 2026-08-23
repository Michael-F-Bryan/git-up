# mfb-git-up

A safe Python Git extension that provides `git up`.

`git up` discovers `origin`'s advertised default branch, fetches and prunes
it, fast-forwards the primary checkout only when that is safe, and removes
clean, unlocked worktrees whose branches are already merged into the fetched
remote default branch. It keeps the caller, primary, detached, locked, dirty,
unmerged, stale-but-locked, and uninspectable worktrees. It never resets,
rebases, merges non-fast-forward, or force-deletes a branch.

Use `--dry-run` to inspect the plan without fetching or changing Git metadata:

```console
git up --dry-run
git up
```

## Install

```console
uv tool install .
git up --help
git up --version
```

## Development

```console
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
```

The project uses a `src/` layout, standard-library runtime code, and a
packaged console-script entry point.
