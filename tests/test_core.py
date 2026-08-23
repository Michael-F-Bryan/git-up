from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest

from git_up.core import (
    CommandResult,
    GitUp,
    GitUpError,
    Worktree,
    classify_worktree,
    default_branch_from_ls_remote,
    parse_worktree_list,
)


class FakeGit:
    def __init__(
        self,
        *,
        dirty_primary: bool = False,
        fail_prefix: tuple[str, ...] | None = None,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], Path | None, bool, bool]] = []
        self.dirty_primary = dirty_primary
        self.fail_prefix = fail_prefix
        self.worktree_lists = 0

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        read_only: bool = False,
    ) -> CommandResult:
        command = tuple(args)
        self.calls.append((command, cwd, check, read_only))
        if self.fail_prefix and command[: len(self.fail_prefix)] == self.fail_prefix:
            if check:
                raise GitUpError(f"simulated failure for {' '.join(command)}")
            return result(b"", returncode=1)
        if command == ("rev-parse", "--show-toplevel"):
            return result(b"/repo\n")
        if command == ("worktree", "list", "--porcelain", "-z"):
            self.worktree_lists += 1
            return result(
                primary_worktree
                if self.worktree_lists == 1
                else b"worktree /repo\0HEAD " + SHA_A.encode() + b"\0"
                b"branch refs/heads/trunk\0"
            )
        if command == ("config", "--get", "remote.origin.url"):
            return result(b"../origin\n")
        if command == ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"):
            return result(b"refs/remotes/origin/trunk\n")
        if command == ("ls-remote", "--symref", "origin", "HEAD"):
            return result(
                b"ref: refs/heads/trunk\tHEAD\n" + SHA_B.encode() + b"\tHEAD\n"
            )
        if command == (
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ):
            return result(
                b" M file\n" if self.dirty_primary and cwd == Path("/repo") else b""
            )
        if command == ("rev-parse", "--verify", "refs/remotes/origin/trunk^{commit}"):
            return result((SHA_B + "\n").encode())
        if command == ("rev-parse", "--verify", "HEAD^{commit}"):
            return result((SHA_A + "\n").encode())
        if command == ("merge-base", "--is-ancestor", SHA_A, SHA_B):
            return result(b"")
        if command == ("submodule", "status", "--recursive"):
            return result(b"")
        return result(b"")


SHA_A = "a" * 40
SHA_B = "b" * 40
primary_worktree = (
    b"worktree /repo\0HEAD "
    + SHA_A.encode()
    + b"\0branch refs/heads/trunk\0"
    + b"\nworktree /repo-wt\0HEAD "
    + SHA_A.encode()
    + b"\0branch refs/heads/merged\0"
)


def result(stdout: bytes, returncode: int = 0) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=b"error")


def test_parse_nul_delimited_worktrees_preserves_lock_and_prunable_reasons() -> None:
    output = (
        b"worktree /repo\0HEAD abc123\0branch refs/heads/main\0"
        b"\nworktree /repo-wt\0HEAD def456\0branch refs/heads/feature\0"
        b"locked by operator\0prunable stale metadata\0"
    )

    worktrees = parse_worktree_list(output)

    assert worktrees == [
        Worktree(path=Path("/repo"), head="abc123", branch="main"),
        Worktree(
            path=Path("/repo-wt"),
            head="def456",
            branch="feature",
            locked=True,
            lock_reason="by operator",
            prunable=True,
            prune_reason="stale metadata",
        ),
    ]


def test_default_branch_is_read_from_symref_not_main_or_master() -> None:
    output = b"ref: refs/heads/trunk\tHEAD\n012345\tHEAD\n"

    assert default_branch_from_ls_remote(output) == "trunk"


@pytest.mark.parametrize(
    ("worktree", "reason"),
    [
        (Worktree(Path("/caller"), "a", "feature"), "current caller"),
        (Worktree(Path("/primary"), "a", "main"), "primary"),
        (Worktree(Path("/detached"), "a", None, detached=True), "detached"),
        (Worktree(Path("/locked"), "a", "feature", locked=True), "locked"),
        (Worktree(Path("/dirty"), "a", "feature"), "dirty"),
        (Worktree(Path("/unmerged"), "b", "feature"), "unmerged"),
        (
            Worktree(Path("/stale"), "a", "feature", prunable=True, locked=True),
            "locked",
        ),
    ],
)
def test_classification_retains_every_defensive_case(
    worktree: Worktree, reason: str
) -> None:
    retained = classify_worktree(
        worktree,
        primary=Path("/primary"),
        caller=Path("/caller"),
        target="a",
        dirty=True if reason == "dirty" else False,
        inspectable=True,
        merged=reason != "unmerged",
    )

    assert retained == reason


def test_classification_retains_uninspectable_worktree() -> None:
    worktree = Worktree(Path("/candidate"), "a", "feature")

    assert (
        classify_worktree(
            worktree,
            primary=Path("/primary"),
            caller=Path("/other"),
            target="a",
            dirty=False,
            inspectable=False,
            merged=True,
        )
        == "uninspectable"
    )


def test_invalid_default_branch_output_is_contextual_error() -> None:
    with pytest.raises(GitUpError, match="default branch"):
        default_branch_from_ls_remote(b"012345\tHEAD\n")


def test_dry_run_uses_only_read_only_git_commands_and_reports_plan() -> None:
    fake = FakeGit()
    output = StringIO()

    code = GitUp(git=fake, cwd=Path("/repo/nested"), stdout=output).run(dry_run=True)

    assert code == 0
    assert "Would fast-forward" in output.getvalue()
    assert "Would remove worktree /repo-wt" in output.getvalue()
    assert "no repository changes" in output.getvalue()
    assert all(read_only for _, _, _, read_only in fake.calls)
    assert not any(
        command[0:2]
        in {
            ("fetch", "--prune"),
            ("worktree", "remove"),
            ("worktree", "prune"),
            ("branch", "-d"),
        }
        for command, _, _, _ in fake.calls
    )


def test_normal_run_fast_forwards_then_removes_only_merged_candidate() -> None:
    fake = FakeGit()
    output = StringIO()

    code = GitUp(git=fake, cwd=Path("/repo"), stdout=output).run()

    assert code == 0
    commands = [call[0] for call in fake.calls]
    assert ("fetch", "--prune", "origin") in commands
    assert ("merge", "--ff-only", "refs/remotes/origin/trunk") in commands
    assert ("worktree", "remove", "--force", "--", "/repo-wt") in commands
    assert ("branch", "-d", "--", "merged") in commands
    assert ("worktree", "prune") in commands
    assert "Updated primary worktree" in output.getvalue()
    assert "Removed worktree /repo-wt" in output.getvalue()


def test_dirty_primary_refuses_before_fetch_or_cleanup() -> None:
    fake = FakeGit(dirty_primary=True)

    code = GitUp(git=fake, cwd=Path("/repo")).run()

    assert code == 1
    commands = [call[0] for call in fake.calls]
    assert ("fetch", "--prune", "origin") not in commands
    assert not any(command[:2] == ("worktree", "prune") for command in commands)


def test_sync_failure_stops_before_worktree_cleanup() -> None:
    fake = FakeGit(fail_prefix=("merge", "--ff-only"))

    code = GitUp(git=fake, cwd=Path("/repo")).run()

    assert code == 1
    commands = [call[0] for call in fake.calls]
    assert not any(command[:2] == ("worktree", "remove") for command in commands)
    assert not any(command[:2] == ("worktree", "prune") for command in commands)


def test_failed_worktree_removal_does_not_delete_its_branch() -> None:
    fake = FakeGit(fail_prefix=("worktree", "remove"))

    code = GitUp(git=fake, cwd=Path("/repo")).run()

    assert code == 1
    commands = [call[0] for call in fake.calls]
    assert ("worktree", "remove", "--force", "--", "/repo-wt") in commands
    assert not any(command[:2] == ("branch", "-d") for command in commands)
