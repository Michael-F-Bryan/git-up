# mfb-git-up

A safe Python Git extension that provides `git up`.

`git up` discovers `origin`'s advertised default branch, fetches and prunes it,
fast-forwards the primary checkout only when that is safe, and removes eligible
merged worktrees. The current caller and the primary worktree are always
retained. Detached, locked, prunable, dirty, unmerged, and uninspectable
worktrees are retained as well. It never resets, rebases, performs a
non-fast-forward merge, or force-deletes a branch.

A removable worktree must be clean in the conservative sense: tracked,
untracked, ignored, and recursively initialized-submodule content all retain the
worktree. The primary checkout is never removed, so ignored build/cache files
alone do not prevent its fast-forward; tracked, untracked, or submodule
modifications do.

Every mutating run takes an atomic cooperative lock in Git's absolute common
directory and releases it on exit. The lock coordinates `git up` processes; it
does not block arbitrary editors or other Git commands. If the lock already
exists, confirm that no `git up` process is running before removing the printed
lock path as stale recovery.

Before any deletion, `git up` builds the complete cleanup plan. Immediately
before the primary fast-forward and each worktree removal it re-reads the
worktree identity, branch, HEAD/ref OIDs, merge reachability, Git worktree lock
state, and complete applicable dirtiness. Changed or unreadable state is
retained or fails closed. Clean initialized-submodule worktrees are handed
straight to Git's `git worktree remove --force`; there is no explicit
submodule-deinitialisation step.

After a worktree is removed, its branch is deleted only when no listed worktree
uses it, with an atomic compare-and-delete against the exact inspected ref OID.
Execution is best effort after full preflight: if a later Git operation fails,
successful earlier removals are not rolled back.

Use `--dry-run` to inspect the current plan without fetching, locking, pruning,
or changing local Git state. Dry-run may query the current remote default through
read-only `git ls-remote --symref origin HEAD`:

```console
git up --dry-run
git up
```

## Install

```console
uv tool install git+https://github.com/Michael-F-Bryan/git-up.git
```

```console
git up -h
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
