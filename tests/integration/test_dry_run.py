"""The dry-run contract: observe, but do not fetch or mutate Git state."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from conftest import GitRepository, GitSandbox


@dataclass(frozen=True)
class RepositoryState:
    """State surfaces that dry-run must leave byte-for-byte/semantically stable."""

    head: str
    refs: str
    fetch_head: bytes | None
    worktrees: str
    branches: frozenset[str]
    paths: tuple[tuple[str, bool], ...]


def _state(repository: GitRepository, paths: list[Path]) -> RepositoryState:
    fetch_head_path = repository.git_dir() / "FETCH_HEAD"
    return RepositoryState(
        head=repository.head(),
        refs=repository.run("show-ref").stdout,
        fetch_head=(fetch_head_path.read_bytes() if fetch_head_path.exists() else None),
        worktrees=repository.worktree_porcelain(),
        branches=frozenset(repository.branch_names()),
        paths=tuple((str(path), path.exists()) for path in paths),
    )


def test_dry_run_does_not_fetch_prune_or_change_repository_state(
    git_sandbox: GitSandbox,
) -> None:
    repository = git_sandbox.repository()
    remote = git_sandbox.bare_remote()
    git_sandbox.link_remote(repository, remote)
    candidate = git_sandbox.linked_worktree(repository, "dry-run-candidate", "dry-run")
    stale = git_sandbox.linked_worktree(repository, "dry-run-stale", "dry-run-stale")
    stale_path = stale.path
    shutil.rmtree(stale_path)
    publisher = git_sandbox.clone(remote, "publisher")
    publisher.write("README.md", "remote but not fetched\n")
    remote_head = publisher.commit("remote dry-run update")
    publisher.run("push", "origin", "HEAD:main")
    before = _state(repository, [repository.path, candidate.path, stale_path])

    result = repository.git_up("--dry-run")

    after = _state(repository, [repository.path, candidate.path, stale_path])
    assert result.returncode == 0
    assert remote_head != before.head
    assert after == before
    assert repository.run("rev-parse", "origin/main").stdout.strip() == before.head
    assert not (repository.git_dir() / "FETCH_HEAD").exists()
    assert candidate.path.exists()
    assert "dry-run-stale" in repository.branch_names()
