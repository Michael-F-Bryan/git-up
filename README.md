# git-up

If you use linked worktrees, you probably have a shell function that does roughly
this: update the main checkout, remove worktrees whose branches have merged, and
delete those branches. It starts small. Then one `git worktree remove` refuses a
worktree that looks clean because it contains an initialised submodule. A script
that carries on to branch deletion produces a second, less useful error. Add
checks for dirty files, ignored build output, locks, detached worktrees, stale
metadata, and worktrees that Git can no longer inspect, and the shortcut has
become a brittle little program with destructive edge cases.

`git up` is that program, with the awkward cases made explicit. It is for the
routine repository reset between pieces of work, not for deciding whether a
branch is safe to merge.

## Why use it instead of an alias?

The useful part is not fetching a branch or running `git worktree remove`. The
useful part is refusing to remove the worktree when its state is ambiguous, and
only deleting the branch after the worktree is gone. For a clean worktree with
an initialised submodule, `git worktree remove` may require `--force`; `git up`
checks the whole worktree first, including nested submodule content, before it
uses that forceful removal.

A typical first run looks like this:

```console
$ git up --dry-run
Would fast-forward primary worktree /src/project to <origin HEAD>.
Would remove worktree /src/project/.worktrees/fix-logging.
Retained worktree /src/project/.worktrees/review: dirty.
Dry run: no repository changes were made.
```

The paths and commit IDs will, of course, be yours.

## What an ordinary run does

From a listed worktree, `git up`:

1. discovers the default branch advertised by `origin`;
2. checks that the primary checkout is on that branch and has no tracked,
   untracked, or submodule modifications;
3. fetches and prunes `origin`, then fast-forwards the primary checkout to the
   fetched remote-tracking branch with `--ff-only`;
4. builds a complete cleanup plan for the other worktrees;
5. rechecks each candidate immediately before acting on it;
6. removes eligible worktrees, then deletes their merged branches; and
7. prunes stale, unlocked worktree metadata.

The current caller and the primary checkout are retained. A worktree is only a
removal candidate when it is inspectable, unlocked, merged into the fetched
remote target, and conservatively clean. That cleanliness includes tracked and
untracked files, ignored files, and recursively initialised submodules. Detached,
locked, prunable, dirty, unmerged, or uninspectable worktrees are left alone.

Branches are removed only after their worktree has been removed, and only when
no other listed worktree uses the branch. Execution is best effort after complete
preflight: if an operation fails, `git up` continues where it safely can and
reports the failure. It does not roll back removals that already succeeded.

## The safety trade-off

`git up` uses `git worktree remove --force` for a candidate that has already
passed its conservative inspection. This is deliberate: Git otherwise refuses
some clean-looking worktrees containing initialised submodules, which is the
failure this tool is meant to handle. The inspection is the guard; `--force` is
not permission to ignore dirty content.

Mutating runs take a small cooperative lock in Git's absolute common directory
so two `git up` processes do not plan against one another. It coordinates
`git up` processes only; it does not block editors or arbitrary Git commands. If
it reports an existing lock, confirm that no `git up` process is running before
removing the printed lock path as stale recovery.

## Install and try it

Requirements are Git, Python 3.11 or newer, and `uv`.

```console
uv tool install git+https://github.com/Michael-F-Bryan/git-up.git
```

Start with the read-only plan. It may query `origin` to discover its current
default branch, but it does not fetch, lock, prune, or change local Git state.

```console
git up --dry-run
git up
git up -h
git up --version
```

A dirty primary checkout refuses the sync on an ordinary run. A divergent
primary checkout is not merged or reset: the fast-forward-only update fails
instead. The remote named `origin` must advertise its default branch through its
`HEAD` symref.

## Development

The project uses a `src/` layout and keeps runtime dependencies in the Python
standard library. Set up the development environment and run the checks with:

```console
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
```

The project is released under the [MIT licence](LICENSE).
