"""Retention rules for worktrees and submodules, using their real on-disk state."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest
from conftest import (
    GitRepository,
    GitSandbox,
    assert_path_is_absent,
    assert_path_is_present,
    snapshot_repository,
)

from git_up.core import CommandResult, GitUp, SubprocessGit


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


def test_merged_detached_worktree_is_removed(git_sandbox: GitSandbox) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.detached_worktree(repository, "detached")

    result = repository.git_up()

    assert result.returncode == 0, result.output
    assert_path_is_absent(candidate.path)
    assert str(candidate.path) not in repository.worktree_porcelain()


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


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("submodule_depth", [0, 1, 2])
def test_merged_worktree_with_ignored_content_is_removed(
    git_sandbox: GitSandbox, detached: bool, submodule_depth: int
) -> None:
    repository, candidate, leaf = _candidate_with_ignored_content(
        git_sandbox, detached=detached, submodule_depth=submodule_depth
    )
    # Exercise ignored content only at the deepest level, not hidden by root dirt.
    parent = candidate.path
    for _ in range(submodule_depth):
        shutil.rmtree(parent / "cache")
        parent /= "vendor/submodule"
    before = snapshot_repository(
        repository, candidates=[candidate], additional_trees=[leaf]
    )

    preview = repository.git_up("--dry-run")

    assert preview.returncode == 0, preview.output
    assert f"Would remove worktree {candidate.path}." in preview.output
    assert (
        snapshot_repository(repository, candidates=[candidate], additional_trees=[leaf])
        == before
    )

    result = repository.git_up()

    assert result.returncode == 0, result.output
    assert_path_is_absent(candidate.path)
    assert "ignored" not in repository.branch_names()
    assert str(candidate.path) not in repository.worktree_porcelain()


def _candidate_with_ignored_content(
    git_sandbox: GitSandbox, *, detached: bool, submodule_depth: int
) -> tuple[GitRepository, GitRepository, GitRepository]:
    repository, _remote = _remote_with_primary(git_sandbox)
    source = repository
    # Build the submodule chain from the inside out.
    for depth in range(submodule_depth):
        nested = source
        source = git_sandbox.make_submodule(f"source-{depth}")
        source.write(".gitignore", "cache/\n")
        source.commit("ignore build output", relative_path=".gitignore")
        if depth:
            git_sandbox.initialise_submodule(source, nested)
    if submodule_depth:
        git_sandbox.initialise_submodule(repository, source)
    repository.write(".gitignore", "cache/\n")
    repository.commit("ignore build output", relative_path=".gitignore")
    repository.run("push", "origin", "HEAD:main")
    candidate = (
        git_sandbox.detached_worktree(repository, "ignored")
        if detached
        else git_sandbox.linked_worktree(repository, "ignored", "ignored")
    )
    candidate.run(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "--recursive",
    )
    tree = candidate
    tree.write("cache/output.txt", "disposable root build output\n")
    for _ in range(submodule_depth):
        tree = GitRepository(tree.path / "vendor/submodule", tree.env)
        tree.write("cache/output.txt", "disposable submodule build output\n")
    return repository, candidate, tree


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("submodule_depth", [0, 1, 2])
@pytest.mark.parametrize("dirt", ["tracked", "untracked"])
def test_nonignored_changes_prevent_removal_despite_ignored_content(
    git_sandbox: GitSandbox, detached: bool, submodule_depth: int, dirt: str
) -> None:
    repository, candidate, leaf = _candidate_with_ignored_content(
        git_sandbox, detached=detached, submodule_depth=submodule_depth
    )
    relative_path = "README.md" if dirt == "tracked" else "notes.txt"
    leaf.write(relative_path, "keep this work\n")
    # Even user-configured submodule suppression must not hide ordinary dirt.
    candidate.run("config", "status.showUntrackedFiles", "no")
    if submodule_depth:
        candidate.run("config", "submodule.vendor/submodule.ignore", "all")

    preview = repository.git_up("--dry-run")
    result = repository.git_up()

    assert preview.returncode == result.returncode == 0, result.output
    for output in (preview.output, result.output):
        assert f"Retained worktree {candidate.path}: dirty." in output
    assert leaf.path.joinpath(relative_path).read_text() == "keep this work\n"
    assert candidate.path.joinpath("cache/output.txt").exists()
    if not detached:
        assert "ignored" in repository.branch_names()


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("reason", ["locked", "unmerged"])
def test_ignored_content_does_not_override_lock_or_unmerged_history(
    git_sandbox: GitSandbox, detached: bool, reason: str
) -> None:
    repository, candidate, _leaf = _candidate_with_ignored_content(
        git_sandbox, detached=detached, submodule_depth=0
    )
    if reason == "locked":
        candidate.run("worktree", "lock", str(candidate.path))
    else:
        candidate.write("README.md", "not in the remote target\n")
        candidate.commit("unmerged work")
    head = candidate.head()

    preview = repository.git_up("--dry-run")
    result = repository.git_up()

    assert preview.returncode == result.returncode == 0, result.output
    for output in (preview.output, result.output):
        assert f"Retained worktree {candidate.path}: {reason}." in output
    assert candidate.head() == head
    assert candidate.path.joinpath("cache/output.txt").exists()
    if not detached:
        assert "ignored" in repository.branch_names()


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("change", ["tracked", "untracked", "head", "locked"])
def test_final_recheck_preserves_changed_candidate_with_ignored_content(
    git_sandbox: GitSandbox,
    monkeypatch: pytest.MonkeyPatch,
    detached: bool,
    change: str,
) -> None:
    repository, candidate, leaf = _candidate_with_ignored_content(
        git_sandbox, detached=detached, submodule_depth=2
    )
    for key, value in git_sandbox.env.items():
        monkeypatch.setenv(key, value)

    class ChangingGit(SubprocessGit):
        lists = 0

        def run(
            self,
            args: Sequence[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
            read_only: bool = False,
        ) -> CommandResult:
            if list(args) == ["worktree", "list", "--porcelain", "-z"]:
                self.lists += 1
                if self.lists == 2:
                    if change == "locked":
                        candidate.run("worktree", "lock", str(candidate.path))
                    elif change == "head":
                        candidate.write("README.md", "new commit during cleanup\n")
                        candidate.commit("late unmerged work")
                    else:
                        relative = "README.md" if change == "tracked" else "notes.txt"
                        leaf.write(relative, "late work in nested submodule\n")
            return super().run(args, cwd=cwd, check=check, read_only=read_only)

    output = StringIO()
    code = GitUp(git=ChangingGit(), cwd=repository.path, stdout=output).run()

    assert code == 0, output.getvalue()
    assert "final recheck" in output.getvalue()
    assert candidate.path.joinpath("cache/output.txt").exists()
    if change in {"tracked", "untracked"}:
        relative = "README.md" if change == "tracked" else "notes.txt"
        assert (
            leaf.path.joinpath(relative).read_text()
            == "late work in nested submodule\n"
        )
    if not detached:
        assert "ignored" in repository.branch_names()


def test_uninspectable_linked_worktree_is_retained_with_useful_reason(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(
        repository, "uninspectable", "uninspectable"
    )
    git_link = candidate.path / ".git"
    original_link = git_link.read_bytes()
    git_link.write_bytes(b"gitdir: /path/that/does/not/exist\n")

    try:
        result = repository.git_up()
    finally:
        git_link.write_bytes(original_link)

    assert result.returncode == 0
    assert_path_is_present(candidate.path)
    assert candidate.current_branch() == "uninspectable"
    assert "uninspectable" in repository.branch_names()
    assert str(candidate.path) in result.output
    assert "uninspectable" in result.output.lower()


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
    assert_path_is_absent(detached.path)
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


def test_stale_unlocked_default_metadata_is_pruned_and_branch_preserved(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    repository.run("switch", "-c", "feature-caller")
    stale_path = git_sandbox.root / "stale-default"
    repository.run("worktree", "add", str(stale_path), "main")
    shutil.rmtree(stale_path)
    candidate = git_sandbox.linked_worktree(repository, "merged-candidate", "merged")

    result = repository.git_up()

    assert result.returncode == 0, result.output
    assert str(stale_path) not in repository.worktree_porcelain()
    assert "main" in repository.branch_names()
    assert not candidate.path.exists()
    assert "merged" not in repository.branch_names()


def test_stale_locked_default_current_allows_housekeeping_and_retains_metadata(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    repository.run("switch", "-c", "feature-caller")
    stale_path = git_sandbox.root / "locked-default"
    repository.run("worktree", "add", str(stale_path), "main")
    stale = GitRepository(stale_path, repository.env)
    lock = stale.run(
        "worktree",
        "lock",
        "--reason",
        "temporarily unavailable",
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
    candidate = git_sandbox.linked_worktree(repository, "merged-candidate", "merged")

    result = repository.git_up()

    assert result.returncode == 0, result.output
    assert str(stale_path) in repository.worktree_porcelain()
    assert "temporarily unavailable" in repository.worktree_porcelain()
    assert "main" in repository.branch_names()
    assert not candidate.path.exists()


def test_stale_locked_default_ahead_refuses_without_advancing_or_cleaning(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    repository.run("switch", "-c", "feature-caller")
    stale_path = git_sandbox.root / "locked-default"
    repository.run("worktree", "add", str(stale_path), "main")
    stale = GitRepository(stale_path, repository.env)
    lock = stale.run(
        "worktree",
        "lock",
        "--reason",
        "temporarily unavailable",
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
    old_main = repository.run("rev-parse", "refs/heads/main").stdout.strip()
    shutil.rmtree(stale_path)
    candidate = git_sandbox.linked_worktree(repository, "merged-candidate", "merged")
    publisher = git_sandbox.clone(remote, "publisher")
    publisher.write("README.md", "remote update\n")
    publisher.commit("advance main")
    publisher.run("push", "origin", "HEAD:main")

    result = repository.git_up()

    assert result.returncode != 0
    assert repository.run("rev-parse", "refs/heads/main").stdout.strip() == old_main
    assert str(stale_path) in repository.worktree_porcelain()
    assert candidate.path.exists()
    assert "merged" in repository.branch_names()


def test_merged_local_branch_without_worktree_is_deleted(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    repository.run("branch", "merged-without-worktree")
    assert "merged-without-worktree" in repository.branch_names()

    result = repository.git_up()

    assert result.returncode == 0, result.output
    assert "merged-without-worktree" not in repository.branch_names()
