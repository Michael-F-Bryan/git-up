"""Real-repository update, fetch, and linked-caller scenarios."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import GitRepository, GitSandbox, remote_default


def _remote_with_primary(
    git_sandbox: GitSandbox, *, branch: str = "main"
) -> tuple[GitRepository, GitRepository]:
    repository = git_sandbox.repository(branch=branch)
    remote = git_sandbox.bare_remote(branch=branch)
    git_sandbox.link_remote(repository, remote, branch=branch)
    return repository, remote


def test_nested_linked_worktree_invocation_updates_primary_and_retains_caller(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    caller = git_sandbox.linked_worktree(repository, "caller", "caller")
    publisher = git_sandbox.clone(remote, "publisher")
    publisher.write("README.md", "published\n")
    published_head = publisher.commit("publish update")
    publisher.run("push", "origin", "HEAD:main")

    result = caller.git_up()

    assert result.returncode == 0
    assert repository.head() == published_head
    assert repository.current_branch() == "main"
    assert caller.path.is_dir()
    assert caller.current_branch() == "caller"
    assert caller.head() != published_head
    assert "caller" in repository.branch_names()


def test_nonstandard_remote_default_branch_is_used_for_fast_forward(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox, branch="trunk")
    publisher = git_sandbox.clone(remote, "publisher", branch="trunk")
    publisher.write("README.md", "trunk update\n")
    published_head = publisher.commit("advance trunk")
    publisher.run("push", "origin", "HEAD:trunk")

    result = repository.git_up()

    assert result.returncode == 0
    assert remote_default(repository) == "trunk"
    assert repository.current_branch() == "trunk"
    assert repository.head() == published_head
    assert repository.path.joinpath("README.md").read_text() == "trunk update\n"


def test_fast_forward_update_moves_primary_to_fetched_remote_head(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    publisher = git_sandbox.clone(remote, "publisher")
    publisher.write("README.md", "fast-forwarded\n")
    published_head = publisher.commit("advance primary")
    publisher.run("push", "origin", "HEAD:main")
    old_head = repository.head()

    result = repository.git_up()

    assert result.returncode == 0
    assert old_head != published_head
    assert repository.head() == published_head
    assert repository.path.joinpath("README.md").read_text() == "fast-forwarded\n"
    assert repository.run("rev-parse", "origin/main").stdout.strip() == published_head


def test_divergence_is_refused_without_merge_or_reset(git_sandbox: GitSandbox) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    publisher = git_sandbox.clone(remote, "publisher")
    publisher.write("remote.txt", "remote\n")
    remote_head = publisher.commit("diverge remotely", relative_path="remote.txt")
    publisher.run("push", "origin", "HEAD:main")
    repository.write("local.txt", "local\n")
    local_head = repository.commit("diverge locally", relative_path="local.txt")

    result = repository.git_up()

    assert result.returncode != 0
    assert repository.head() == local_head
    assert repository.path.joinpath("local.txt").read_text() == "local\n"
    assert not repository.path.joinpath("remote.txt").exists()
    assert repository.run("rev-list", "--merges", "-1", "HEAD").stdout.strip() == ""
    assert repository.run("rev-parse", "origin/main").stdout.strip() == remote_head


def test_fetch_prune_removes_deleted_remote_tracking_branch(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    initial_head = repository.head()
    remote.run("update-ref", "refs/heads/obsolete", initial_head)
    repository.run("fetch", "origin", "obsolete:refs/remotes/origin/obsolete")
    assert "origin/obsolete" in repository.remote_branch_names()
    remote.run("update-ref", "-d", "refs/heads/obsolete")

    result = repository.git_up()

    assert result.returncode == 0
    assert "origin/obsolete" not in repository.remote_branch_names()
    assert "origin/main" in repository.remote_branch_names()


def test_repeated_invocation_is_idempotent_after_cleanup(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(repository, "merged-candidate", "merged")
    candidate_branch = candidate.current_branch()
    assert candidate_branch == "merged"
    first = repository.git_up()
    first_state = repository.worktree_porcelain()

    second = repository.git_up()

    assert first.returncode == 0
    assert second.returncode == 0
    assert not candidate.path.exists()
    assert candidate_branch not in repository.branch_names()
    assert repository.worktree_porcelain() == first_state


def test_path_with_spaces_is_handled_as_one_worktree_path(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(
        repository, "work tree with spaces", "spaced"
    )
    assert Path(candidate.path).exists()

    result = repository.git_up()

    assert result.returncode == 0
    assert not candidate.path.exists()
    assert "spaced" not in repository.branch_names()


def test_posix_worktree_path_ending_in_newline_is_preserved(
    git_sandbox: GitSandbox,
) -> None:
    if os.name != "posix":
        pytest.skip("newline worktree path contract is POSIX-only")
    repository, _remote = _remote_with_primary(git_sandbox)
    newline_name = "worktree-ending-in-newline\n"
    try:
        candidate = git_sandbox.linked_worktree(
            repository, newline_name, "newline-path"
        )
    except AssertionError as error:
        if "invalid" in str(error).lower() or "unsupported" in str(error).lower():
            pytest.skip("this Git does not support a newline worktree path")
        raise

    result = repository.git_up()

    assert result.returncode == 0
    assert not candidate.path.exists()
    assert "newline-path" not in repository.branch_names()
    assert str(candidate.path) in result.output


def test_dirty_feature_primary_updates_unchecked_out_default_and_preserves_caller(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(repository, "merged-candidate", "merged")
    repository.run("switch", "-c", "feature-caller")
    repository.write("README.md", "caller tracked dirt\n")
    repository.write("caller-untracked.txt", "caller untracked dirt\n")
    before_head = repository.head()
    before_branch = repository.current_branch()
    before_index = repository.run(
        "rev-parse", "--path-format=absolute", "--git-path", "index"
    ).stdout.strip()
    before_index_bytes = Path(before_index).read_bytes()
    before_files = (
        repository.path.joinpath("README.md").read_bytes(),
        repository.path.joinpath("caller-untracked.txt").read_bytes(),
    )
    before_status = repository.run(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ).stdout
    publisher = git_sandbox.clone(remote, "publisher")
    publisher.write("README.md", "remote update\n")
    published_head = publisher.commit("advance main")
    publisher.run("push", "origin", "HEAD:main")

    result = repository.git_up()

    assert result.returncode == 0, result.output
    assert repository.current_branch() == before_branch == "feature-caller"
    assert repository.head() == before_head
    assert Path(before_index).read_bytes() == before_index_bytes
    assert (
        repository.path.joinpath("README.md").read_bytes(),
        repository.path.joinpath("caller-untracked.txt").read_bytes(),
    ) == before_files
    assert (
        repository.run(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ).stdout
        == before_status
    )
    assert (
        repository.run("rev-parse", "refs/heads/main").stdout.strip() == published_head
    )
    assert not candidate.path.exists()
    assert "merged" not in repository.branch_names()


def test_clean_checked_out_default_worktree_updates_without_switching_caller(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    repository.run("switch", "-c", "feature-caller")
    default_path = git_sandbox.root / "default-worktree"
    repository.run("worktree", "add", str(default_path), "main")
    default = GitRepository(default_path, repository.env)
    publisher = git_sandbox.clone(remote, "publisher")
    publisher.write("README.md", "remote default update\n")
    published_head = publisher.commit("advance main")
    publisher.run("push", "origin", "HEAD:main")
    caller_head = repository.head()

    result = repository.git_up()

    assert result.returncode == 0, result.output
    assert repository.current_branch() == "feature-caller"
    assert repository.head() == caller_head
    assert default.current_branch() == "main"
    assert default.head() == published_head
    assert default.path.joinpath("README.md").read_text() == "remote default update\n"


def test_dirty_checked_out_default_worktree_fails_without_desynchronising_it(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    repository.run("switch", "-c", "feature-caller")
    default_path = git_sandbox.root / "dirty-default-worktree"
    repository.run("worktree", "add", str(default_path), "main")
    default = GitRepository(default_path, repository.env)
    default.write("README.md", "dirty default\n")
    before_head = default.head()
    before_index_path = default.run(
        "rev-parse", "--path-format=absolute", "--git-path", "index"
    ).stdout.strip()
    before_index = Path(before_index_path).read_bytes()
    before_status = default.run(
        "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
    ).stdout
    publisher = git_sandbox.clone(remote, "publisher")
    publisher.write("README.md", "remote default update\n")
    publisher.commit("advance main")
    publisher.run("push", "origin", "HEAD:main")

    result = repository.git_up()

    assert result.returncode != 0
    assert repository.current_branch() == "feature-caller"
    assert default.current_branch() == "main"
    assert default.head() == before_head
    assert Path(before_index_path).read_bytes() == before_index
    assert (
        default.run(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ).stdout
        == before_status
    )
    assert repository.run("rev-parse", "refs/heads/main").stdout.strip() == before_head


def test_dirty_checked_out_default_already_current_allows_housekeeping(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    caller = git_sandbox.linked_worktree(repository, "caller", "caller")
    candidate = git_sandbox.linked_worktree(repository, "merged-candidate", "merged")
    repository.write("README.md", "dirty default\n")
    repository.write("default-untracked.txt", "preserve me\n")
    before_head = repository.head()
    before_branch = repository.current_branch()
    before_index_path = repository.run(
        "rev-parse", "--path-format=absolute", "--git-path", "index"
    ).stdout.strip()
    before_index = Path(before_index_path).read_bytes()
    before_files = (
        repository.path.joinpath("README.md").read_bytes(),
        repository.path.joinpath("default-untracked.txt").read_bytes(),
    )
    before_status = repository.run(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ).stdout

    result = caller.git_up()

    assert result.returncode == 0, result.output
    assert repository.current_branch() == before_branch == "main"
    assert repository.head() == before_head
    assert Path(before_index_path).read_bytes() == before_index
    assert (
        repository.path.joinpath("README.md").read_bytes(),
        repository.path.joinpath("default-untracked.txt").read_bytes(),
    ) == before_files
    assert (
        repository.run(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ).stdout
        == before_status
    )
    assert not candidate.path.exists()
    assert caller.path.exists()


def test_dry_run_refuses_dirty_default_when_known_target_is_ahead(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    repository.write("README.md", "dirty default\n")
    publisher = git_sandbox.clone(remote, "publisher")
    publisher.write("README.md", "remote update\n")
    publisher.commit("advance main")
    publisher.run("push", "origin", "HEAD:main")
    repository.run("fetch", "origin", "main")
    before_status = repository.run(
        "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
    ).stdout

    result = repository.git_up("--dry-run")

    assert result.returncode != 0
    assert "dirty" in result.output.lower()
    assert "would fast-forward" not in result.output.lower()
    assert (
        repository.run(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ).stdout
        == before_status
    )


def test_dry_run_dirty_current_default_shows_housekeeping_plan(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    candidate = git_sandbox.linked_worktree(repository, "merged-candidate", "merged")
    repository.write("README.md", "dirty default\n")

    result = repository.git_up("--dry-run")

    assert result.returncode == 0, result.output
    assert "already up to date" in result.output
    assert f"Would remove worktree {candidate.path}" in result.output
    assert "no repository changes" in result.output


def test_dirty_feature_caller_creates_missing_local_default_ref(
    git_sandbox: GitSandbox,
) -> None:
    repository, _remote = _remote_with_primary(git_sandbox)
    target = repository.run("rev-parse", "origin/main").stdout.strip()
    repository.run("switch", "-c", "feature-caller")
    repository.write("README.md", "dirty caller\n")
    repository.write("caller-untracked.txt", "keep caller dirt\n")
    before_head = repository.head()
    before_status = repository.run(
        "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
    ).stdout
    repository.run("update-ref", "-d", "refs/heads/main")

    result = repository.git_up()

    assert result.returncode == 0, result.output
    assert repository.current_branch() == "feature-caller"
    assert repository.head() == before_head
    assert repository.run("rev-parse", "refs/heads/main").stdout.strip() == target
    assert (
        repository.run(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ).stdout
        == before_status
    )


def test_divergent_unchecked_out_default_is_refused_without_reset_or_merge(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = _remote_with_primary(git_sandbox)
    repository.write("local-only.txt", "local\n")
    local_default_head = repository.commit(
        "diverge local main", relative_path="local-only.txt"
    )
    publisher = git_sandbox.clone(remote, "publisher")
    publisher.write("remote-only.txt", "remote\n")
    remote_head = publisher.commit(
        "diverge remote main", relative_path="remote-only.txt"
    )
    publisher.run("push", "origin", "HEAD:main")
    repository.run("switch", "-c", "feature-caller")

    result = repository.git_up()

    assert result.returncode != 0
    assert repository.current_branch() == "feature-caller"
    assert (
        repository.run("rev-parse", "refs/heads/main").stdout.strip()
        == local_default_head
    )
    assert repository.run("rev-parse", "origin/main").stdout.strip() == remote_head
    assert (
        repository.run("rev-list", "--merges", "-1", "refs/heads/main").stdout.strip()
        == ""
    )
    assert not repository.path.joinpath("remote-only.txt").exists()
