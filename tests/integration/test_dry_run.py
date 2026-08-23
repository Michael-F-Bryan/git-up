"""The dry-run contract: observe, but do not fetch or mutate Git state."""

from __future__ import annotations

import shutil

from conftest import GitRepository, GitSandbox, snapshot_repository


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
    before = snapshot_repository(repository, candidates=[candidate])

    result = repository.git_up("--dry-run")

    after = snapshot_repository(repository, candidates=[candidate])
    assert result.returncode == 0
    assert remote_head != before.head
    assert after == before
    assert repository.run("rev-parse", "origin/main").stdout.strip() == before.head
    assert after.fetch_head is None
    assert candidate.path.exists()
    assert "dry-run-stale" in repository.branch_names()


def test_dry_run_preserves_mixed_dirty_ignored_and_initialized_submodule_state(
    git_sandbox: GitSandbox,
) -> None:
    repository, remote = git_sandbox.repository(), git_sandbox.bare_remote()
    git_sandbox.link_remote(repository, remote)
    source = git_sandbox.make_submodule("mixed-source")
    source.write(".gitignore", "ignored-inside-submodule.txt\n")
    source.commit("ignore submodule fixture file", relative_path=".gitignore")
    repository.write(".gitignore", "ignored-candidate.txt\n")
    repository.commit("add ignore rules", relative_path=".gitignore")
    git_sandbox.initialise_submodule(repository, source)
    repository.run("push", "origin", "HEAD:main")
    repository.run("fetch", "origin")
    repository.git_dir().joinpath("ORIG_HEAD").write_bytes(b"dry-run ORIG_HEAD\n")

    clean = git_sandbox.linked_worktree(repository, "dry-clean", "dry-clean")
    dirty = git_sandbox.linked_worktree(repository, "dry-dirty", "dry-dirty")
    dirty.write("README.md", "tracked dirt\n")
    dirty.write("untracked.txt", "untracked dirt\n")
    dirty.write("ignored-candidate.txt", "ignored dirt\n")
    dirty.run("-c", "protocol.file.allow=always", "submodule", "update", "--init")
    dirty_submodule = GitRepository(dirty.path / "vendor/submodule", dirty.env)
    dirty_submodule.write("ignored-inside-submodule.txt", "ignored submodule dirt\n")
    stale = git_sandbox.linked_worktree(repository, "dry-stale", "dry-stale")
    stale_path = stale.path
    shutil.rmtree(stale_path)
    lock = repository.git_dir() / "fixture.lock"
    lock.write_bytes(b"dry-run lock fixture\n")

    before = snapshot_repository(
        repository,
        candidates=[clean, dirty, stale],
        additional_trees=[dirty_submodule],
    )
    result = repository.git_up("--dry-run")
    after = snapshot_repository(
        repository,
        candidates=[clean, dirty, stale],
        additional_trees=[dirty_submodule],
    )

    assert result.returncode == 0
    assert after == before
    assert before.fetch_head is not None
    assert before.orig_head == b"dry-run ORIG_HEAD\n"
    assert "ignored-candidate.txt" in before.status
    assert "ignored-inside-submodule.txt" in before.status
    assert any(
        relative == "ignored-candidate.txt" and content == b"ignored dirt\n"
        for path, content in before.files
        for relative in [path.rsplit(":", maxsplit=1)[-1]]
    )
    assert any(
        relative == "ignored-inside-submodule.txt"
        and content == b"ignored submodule dirt\n"
        for path, content in before.files
        for relative in [path.rsplit(":", maxsplit=1)[-1]]
    )
    assert "dry-clean" in result.output
    assert "dry-dirty" in result.output
    assert "dry-stale" in repository.branch_names()
