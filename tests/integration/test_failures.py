"""Failure boundaries: fetch must precede cleanup, and ref failures are visible."""

from __future__ import annotations

import shutil

from conftest import GitRepository, GitSandbox


def _remote_with_primary(
    git_sandbox: GitSandbox,
) -> tuple[GitRepository, GitRepository]:
    repository = git_sandbox.repository()
    remote = git_sandbox.bare_remote()
    git_sandbox.link_remote(repository, remote)
    return repository, remote


def test_fetch_failure_prevents_worktree_and_branch_cleanup(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(
        repository, "fetch-failure", "fetch-failure"
    )
    stale = git_sandbox.linked_worktree(
        repository, "fetch-failure-stale", "fetch-failure-stale"
    )
    stale_path = stale.path
    shutil.rmtree(stale_path)
    old_head = repository.head()
    repository.run(
        "remote", "set-url", "origin", str(git_sandbox.root / "missing-remote")
    )

    result = repository.git_up()

    assert result.returncode != 0
    assert repository.head() == old_head
    assert candidate.path.exists()
    assert "fetch-failure" in repository.branch_names()
    assert "fetch-failure-stale" in repository.worktree_porcelain()


def test_branch_is_retained_when_real_ref_lock_blocks_removal(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(
        repository, "blocked-removal", "blocked-removal"
    )
    ref_lock = repository.git_dir() / "refs/heads/blocked-removal.lock"
    ref_lock.parent.mkdir(parents=True, exist_ok=True)
    ref_lock.write_text("held by another real Git operation\n", encoding="utf-8")

    result = repository.git_up()

    assert result.returncode != 0
    assert "blocked-removal" in repository.branch_names()
    assert (
        "blocked-removal" in repository.worktree_porcelain()
        or not candidate.path.exists()
    )
    assert ref_lock.exists()
