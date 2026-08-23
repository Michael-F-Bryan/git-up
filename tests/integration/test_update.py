"""Real-repository update, fetch, and linked-caller scenarios."""

from __future__ import annotations

from pathlib import Path

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
