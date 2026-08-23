"""Contract tests for Git's external ``git up`` command dispatch."""

from __future__ import annotations

from conftest import GitSandbox


def test_git_external_dispatch_reaches_product_help(git_sandbox: GitSandbox) -> None:
    repository = git_sandbox.repository()

    # ``git up --help`` is intercepted by Git as a man-page request.  ``-h``
    # is the external command path and still exercises the product help.
    result = repository.git_up("-h")

    assert result.returncode == 0
    assert "usage" in result.output.lower()
    assert "worktree" in result.output.lower()


def test_git_external_dispatch_reports_unknown_option_from_product(
    git_sandbox: GitSandbox,
) -> None:
    repository = git_sandbox.repository()

    result = repository.git_up("--definitely-not-a-git-up-option")

    assert result.returncode != 0
    assert "git-up" in result.output.lower()
