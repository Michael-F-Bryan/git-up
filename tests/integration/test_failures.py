"""Failure boundaries: fetch must precede cleanup, and ref failures are visible."""

from __future__ import annotations

import shutil

import pytest
from conftest import GitRepository, GitSandbox, snapshot_repository


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


@pytest.mark.parametrize(
    ("dirty_name", "dirty_content"),
    [
        ("tracked-primary-dirt", "tracked primary bytes\n"),
        ("untracked-primary-dirt", "untracked primary bytes\n"),
    ],
)
def test_dirty_primary_current_target_allows_housekeeping_without_changing_checkout(
    git_sandbox: GitSandbox,
    dirty_name: str,
    dirty_content: str,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    repository.run("fetch", "origin")
    candidate = git_sandbox.linked_worktree(
        repository, "primary-safety", "primary-safety"
    )
    stale = git_sandbox.linked_worktree(
        repository, "primary-safety-stale", "primary-safety-stale"
    )
    stale_path = stale.path
    shutil.rmtree(stale_path)
    if dirty_name.startswith("tracked"):
        repository.write("README.md", dirty_content)
    else:
        repository.write("untracked-primary.txt", dirty_content)

    before = snapshot_repository(repository, candidates=[candidate, stale])
    result = repository.git_up()
    after = snapshot_repository(repository, candidates=[candidate, stale])

    assert result.returncode == 0, result.output
    assert after.head == before.head
    assert after.status == before.status
    assert after.indices[0] == before.indices[0]
    assert candidate.path is not None and not candidate.path.exists()
    assert "primary-safety" not in repository.branch_names()
    assert str(stale_path) not in repository.worktree_porcelain()
    assert "already up to date" in result.output


def test_ignored_only_primary_state_does_not_block_safe_update(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    repository.write(".gitignore", "primary-ignored.txt\n")
    repository.commit("add primary ignore rule", relative_path=".gitignore")
    repository.run("push", "origin", "HEAD:main")
    repository.write("primary-ignored.txt", "preserve ignored primary bytes\n")
    publisher = git_sandbox.clone(remote, "ignored-primary-publisher")
    publisher.write("README.md", "safe update with ignored primary\n")
    published_head = publisher.commit("advance remote with ignored primary")
    publisher.run("push", "origin", "HEAD:main")

    result = repository.git_up()

    assert result.returncode == 0
    assert repository.head() == published_head
    assert repository.path.joinpath("primary-ignored.txt").read_text() == (
        "preserve ignored primary bytes\n"
    )


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


def test_later_real_ref_failure_keeps_failed_ref_after_earlier_cleanup(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    first = git_sandbox.linked_worktree(repository, "first-cleanup", "first-cleanup")
    second = git_sandbox.linked_worktree(
        repository, "second-ref-failure", "second-ref-failure"
    )
    ref_lock = repository.git_dir() / "refs/heads/second-ref-failure.lock"
    ref_lock.parent.mkdir(parents=True, exist_ok=True)
    ref_lock.write_bytes(b"held by another real Git operation\n")

    result = repository.git_up()

    assert result.returncode != 0
    assert not first.path.exists()
    assert "first-cleanup" not in repository.branch_names()
    assert not second.path.exists()
    assert "second-ref-failure" in repository.branch_names()
    assert "second-ref-failure" not in repository.worktree_porcelain()
    assert ref_lock.read_bytes() == b"held by another real Git operation\n"
    assert "second-ref-failure" in result.output


def test_first_real_ref_failure_does_not_block_later_branch_cleanup(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    first = git_sandbox.linked_worktree(
        repository, "first-ref-failure", "first-ref-failure"
    )
    second = git_sandbox.linked_worktree(repository, "second-cleanup", "second-cleanup")
    ref_lock = repository.git_dir() / "refs/heads/first-ref-failure.lock"
    ref_lock.parent.mkdir(parents=True, exist_ok=True)
    ref_lock.write_bytes(b"held by another real Git operation\n")

    result = repository.git_up()

    assert result.returncode != 0
    assert not first.path.exists()
    assert "first-ref-failure" in repository.branch_names()
    assert not second.path.exists()
    assert "second-cleanup" not in repository.branch_names()
    assert ref_lock.read_bytes() == b"held by another real Git operation\n"
    assert "first-ref-failure" in result.output
    assert "Deleted merged branch second-cleanup." in result.output


def test_preflight_invalid_candidate_does_not_block_earlier_cleanup(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    first = git_sandbox.linked_worktree(
        repository, "preflight-first", "preflight-first"
    )
    invalid = git_sandbox.linked_worktree(
        repository, "preflight-invalid", "preflight-invalid"
    )
    git_link = invalid.path / ".git"
    original_link = git_link.read_bytes()
    git_link.write_bytes(b"gitdir: /preflight/path/does/not/exist\n")

    try:
        result = repository.git_up()
    finally:
        git_link.write_bytes(original_link)

    assert result.returncode == 0
    assert not first.path.exists()
    assert "preflight-first" not in repository.branch_names()
    assert invalid.path.exists()
    assert "preflight-invalid" in repository.branch_names()
    assert str(invalid.path) in result.output
    assert "uninspectable" in result.output.lower()


def test_existing_cooperative_lock_blocks_mutation_but_not_dry_run(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(
        repository, "cooperative-lock", "cooperative-lock"
    )
    lock = repository.git_dir() / "git-up.lock"
    lock.write_bytes(b"owned by another git up\n")

    before = snapshot_repository(repository, candidates=[candidate])
    dry_run = repository.git_up("--dry-run")
    after_dry_run = snapshot_repository(repository, candidates=[candidate])
    result = repository.git_up()
    after = snapshot_repository(repository, candidates=[candidate])

    assert dry_run.returncode == 0
    assert after_dry_run == before
    assert result.returncode != 0
    assert after == before
    assert lock.read_bytes() == b"owned by another git up\n"
    assert candidate.path.exists()
    assert "lock" in result.output.lower()
