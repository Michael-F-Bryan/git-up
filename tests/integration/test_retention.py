"""Retention rules for worktrees and submodules, using their real on-disk state."""

from __future__ import annotations

import shutil

import pytest
from conftest import (
    GitRepository,
    GitSandbox,
    assert_path_is_absent,
    assert_path_is_present,
)


def _remote_with_primary(
    git_sandbox: GitSandbox,
) -> tuple[GitRepository, GitRepository]:
    repository = git_sandbox.repository()
    remote = git_sandbox.bare_remote()
    git_sandbox.link_remote(repository, remote)
    return repository, remote


def test_dirty_tracked_worktree_is_retained(git_sandbox: GitSandbox) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(
        repository, "dirty-tracked", "dirty-tracked"
    )
    candidate.write("README.md", "changed but uncommitted\n")

    result = repository.git_up()

    assert result.returncode == 0
    assert_path_is_present(candidate.path)
    assert candidate.current_branch() == "dirty-tracked"
    assert "dirty-tracked" in repository.branch_names()


def test_untracked_file_worktree_is_retained(git_sandbox: GitSandbox) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(
        repository, "dirty-untracked", "dirty-untracked"
    )
    candidate.write("untracked.txt", "keep me\n")

    result = repository.git_up()

    assert result.returncode == 0
    assert_path_is_present(candidate.path)
    assert candidate.path.joinpath("untracked.txt").read_text() == "keep me\n"
    assert "dirty-untracked" in repository.branch_names()


def test_locked_worktree_is_retained_with_reason(git_sandbox: GitSandbox) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(repository, "locked", "locked")
    lock = candidate.run(
        "worktree",
        "lock",
        "--reason",
        "keep for review",
        str(candidate.path),
        check=False,
    )
    if lock.returncode != 0 and (
        "unknown" in lock.output.lower()
        or "not a git worktree command" in lock.output.lower()
    ):
        pytest.skip("this Git does not support worktree locking")
    assert lock.returncode == 0, lock.output

    result = repository.git_up()

    assert result.returncode == 0
    assert_path_is_present(candidate.path)
    assert "locked" in repository.worktree_porcelain()
    assert "keep for review" in repository.worktree_porcelain()


def test_unmerged_clean_worktree_is_retained(git_sandbox: GitSandbox) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(repository, "unmerged", "unmerged")
    candidate.write("unmerged.txt", "not in origin\n")
    candidate.commit("unmerged work", relative_path="unmerged.txt")

    result = repository.git_up()

    assert result.returncode == 0
    assert_path_is_present(candidate.path)
    assert candidate.current_branch() == "unmerged"
    assert "unmerged" in repository.branch_names()


def test_detached_worktree_is_retained(git_sandbox: GitSandbox) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.detached_worktree(repository, "detached")
    detached_head = candidate.head()

    result = repository.git_up()

    assert result.returncode == 0
    assert_path_is_present(candidate.path)
    assert candidate.current_branch() is None
    assert candidate.head() == detached_head


def test_clean_initialised_submodule_worktree_is_removed(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    source = git_sandbox.make_submodule()
    git_sandbox.initialise_submodule(repository, source)
    repository.run("push", "origin", "HEAD:main")
    candidate = git_sandbox.linked_worktree(
        repository, "clean-submodule", "clean-submodule"
    )
    candidate.run(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
    )
    assert candidate.path.joinpath("vendor/submodule/.git").exists()

    result = repository.git_up()

    assert result.returncode == 0
    assert_path_is_absent(candidate.path)
    assert "clean-submodule" not in repository.branch_names()
    assert (
        "main"
        in remote.run("for-each-ref", "--format=%(refname:short)", "refs/heads").stdout
    )


def test_dirty_submodule_worktree_is_retained(git_sandbox: GitSandbox) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    source = git_sandbox.make_submodule()
    git_sandbox.initialise_submodule(repository, source)
    repository.run("push", "origin", "HEAD:main")
    candidate = git_sandbox.linked_worktree(
        repository, "dirty-submodule", "dirty-submodule"
    )
    candidate.run(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
    )
    submodule = GitRepository(candidate.path / "vendor/submodule", candidate.env)
    submodule.write("README.md", "dirty submodule\n")

    result = repository.git_up()

    assert result.returncode == 0
    assert_path_is_present(candidate.path)
    assert submodule.path.joinpath("README.md").read_text() == "dirty submodule\n"
    assert "dirty-submodule" in repository.branch_names()


def test_mixed_worktree_set_removes_only_eligible_clean_branch(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    clean = git_sandbox.linked_worktree(repository, "clean", "clean")
    tracked = git_sandbox.linked_worktree(repository, "tracked", "tracked")
    tracked.write("README.md", "tracked dirty\n")
    untracked = git_sandbox.linked_worktree(repository, "untracked", "untracked")
    untracked.write("new.txt", "untracked dirty\n")
    locked = git_sandbox.linked_worktree(repository, "locked-mixed", "locked-mixed")
    lock = locked.run(
        "worktree", "lock", "--reason", "operator hold", str(locked.path), check=False
    )
    if lock.returncode != 0 and (
        "unknown" in lock.output.lower()
        or "not a git worktree command" in lock.output.lower()
    ):
        pytest.skip("this Git does not support worktree locking")
    assert lock.returncode == 0, lock.output
    unmerged = git_sandbox.linked_worktree(
        repository, "unmerged-mixed", "unmerged-mixed"
    )
    unmerged.write("branch.txt", "unmerged\n")
    unmerged.commit("unmerged mixed work", relative_path="branch.txt")
    detached = git_sandbox.detached_worktree(repository, "detached-mixed")

    result = repository.git_up()

    assert result.returncode == 0
    assert_path_is_absent(clean.path)
    assert_path_is_present(tracked.path)
    assert_path_is_present(untracked.path)
    assert_path_is_present(locked.path)
    assert_path_is_present(unmerged.path)
    assert_path_is_present(detached.path)
    assert "clean" not in repository.branch_names()
    assert {
        "tracked",
        "untracked",
        "locked-mixed",
        "unmerged-mixed",
    } <= repository.branch_names()


def test_stale_unlocked_worktree_metadata_is_pruned(git_sandbox: GitSandbox) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    stale = git_sandbox.linked_worktree(repository, "stale", "stale")
    stale_path = stale.path
    shutil.rmtree(stale_path)
    assert str(stale_path) in repository.worktree_porcelain()

    result = repository.git_up()

    assert result.returncode == 0
    assert str(stale_path) not in repository.worktree_porcelain()
    assert "stale" in repository.branch_names()


def test_locked_stale_worktree_metadata_is_retained(git_sandbox: GitSandbox) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    stale = git_sandbox.linked_worktree(repository, "locked-stale", "locked-stale")
    lock = stale.run(
        "worktree",
        "lock",
        "--reason",
        "preserve stale metadata",
        str(stale.path),
        check=False,
    )
    if lock.returncode != 0 and (
        "unknown" in lock.output.lower()
        or "not a git worktree command" in lock.output.lower()
    ):
        pytest.skip("this Git does not support worktree locking")
    assert lock.returncode == 0, lock.output
    stale_path = stale.path
    shutil.rmtree(stale_path)

    result = repository.git_up()

    assert result.returncode == 0
    assert str(stale_path) in repository.worktree_porcelain()
    assert "preserve stale metadata" in repository.worktree_porcelain()
    assert "locked-stale" in repository.branch_names()
