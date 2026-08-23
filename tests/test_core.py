from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest

from git_up.core import (
    CommandResult,
    GitUp,
    GitUpError,
    RepositoryLock,
    Worktree,
    classify_worktree,
    default_branch_from_ls_remote,
    parse_gitlink_paths,
    parse_worktree_list,
)


class FakeGit:
    def __init__(
        self,
        *,
        dirty_primary: bool = False,
        fail_prefix: tuple[str, ...] | None = None,
        ignored_candidate: bool = False,
        initialized_submodule: bool = False,
        dirty_submodule: bool = False,
        candidate_path: Path = Path("/repo-wt"),
        change_on_final_recheck: bool = False,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], Path | None, bool, bool]] = []
        self.dirty_primary = dirty_primary
        self.fail_prefix = fail_prefix
        self.ignored_candidate = ignored_candidate
        self.initialized_submodule = initialized_submodule
        self.dirty_submodule = dirty_submodule
        self.candidate_path = candidate_path
        self.submodule_path = candidate_path / "vendor/submodule"
        self.change_on_final_recheck = change_on_final_recheck
        self.worktree_lists = 0
        self.removed = False
        self.common_dir = Path("/tmp") / f"git-up-test-{id(self)}"
        self.common_dir.mkdir(exist_ok=True)

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
        if command == ("rev-parse", "--git-common-dir"):
            return result((str(self.common_dir) + "\n").encode())
        if command == ("worktree", "list", "--porcelain", "-z"):
            self.worktree_lists += 1
            if self.change_on_final_recheck and self.worktree_lists >= 3:
                changed = (
                    b"worktree /repo\0HEAD "
                    + SHA_A.encode()
                    + b"\0branch refs/heads/trunk\0"
                    b"\nworktree /repo-wt\0HEAD "
                    + SHA_B.encode()
                    + b"\0branch refs/heads/merged\0"
                )
                return result(changed)
            return result(
                b"worktree /repo\0HEAD " + SHA_A.encode() + b"\0"
                b"branch refs/heads/trunk\0"
                if self.removed
                else primary_worktree
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
        if command == (
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
            "--ignored=matching",
            "-z",
        ):
            if self.dirty_submodule and cwd == self.submodule_path:
                return result(b"!! ignored-inside-submodule.txt\0")
            if self.ignored_candidate and cwd == self.candidate_path:
                return result(b"!! cache/\0")
            return result(b"")
        if command == ("ls-files", "--stage", "-z"):
            if self.initialized_submodule and cwd == self.candidate_path:
                return result(b"160000 " + SHA_A.encode() + b" 0\tvendor/submodule\0")
            return result(b"")
        if command == ("rev-parse", "--git-dir") and cwd == self.submodule_path:
            return result(b".git\n")
        if command == ("rev-parse", "--verify", "refs/remotes/origin/trunk^{commit}"):
            return result((SHA_B + "\n").encode())
        if command == ("rev-parse", "--verify", "HEAD^{commit}"):
            return result((SHA_A + "\n").encode())
        if command in {
            ("rev-parse", "--verify", "refs/heads/trunk^{commit}"),
            ("rev-parse", "--verify", "refs/heads/merged^{commit}"),
        }:
            return result((SHA_A + "\n").encode())
        if command == ("merge-base", "--is-ancestor", SHA_A, SHA_B):
            return result(b"")
        if command == ("submodule", "status", "--recursive"):
            return result(b"")
        if command[:2] == ("worktree", "remove"):
            self.removed = True
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


def test_parse_nul_delimited_worktree_preserves_literal_newlines() -> None:
    output = (
        b"worktree /repo/line\n\0HEAD abc123\0branch refs/heads/feature\0"
        b"\nworktree /repo/other\0HEAD def456\0branch refs/heads/other\0"
        b"locked reason ending in newline\n\0"
    )

    worktrees = parse_worktree_list(output)

    assert worktrees[0].path == Path("/repo/line\n")
    assert worktrees[1].lock_reason == "reason ending in newline\n"


def test_parse_gitlink_paths_preserves_literal_path_bytes() -> None:
    output = b"160000 " + SHA_A.encode() + b" 0\tvendor/with\tliteral\nname\0"

    assert parse_gitlink_paths(output) == ["vendor/with\tliteral\nname"]


def test_parse_gitlink_paths_rejects_malformed_records() -> None:
    with pytest.raises(GitUpError, match="staged-file"):
        parse_gitlink_paths(b"not a staged-file record\0")


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
            dirty=False,
            inspectable=False,
            merged=True,
        )
        == "uninspectable"
    )


def test_classification_retains_ignored_content_as_dirty() -> None:
    worktree = Worktree(Path("/candidate"), "a", "feature")

    assert (
        classify_worktree(
            worktree,
            primary=Path("/primary"),
            caller=Path("/other"),
            dirty=True,
            inspectable=True,
            merged=True,
        )
        == "dirty"
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
    assert not fake.common_dir.joinpath(RepositoryLock.filename).exists()
    assert ("ls-remote", "--symref", "origin", "HEAD") in {
        call[0] for call in fake.calls
    }
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
    assert ("update-ref", "-d", "refs/heads/merged", SHA_A) in commands
    assert not any(command[:2] == ("branch", "-d") for command in commands)
    assert not any(command[:2] == ("submodule", "deinit") for command in commands)
    assert not any(command[:2] == ("submodule", "status") for command in commands)
    assert ("worktree", "prune") in commands
    assert "Updated primary worktree" in output.getvalue()
    assert "Removed worktree /repo-wt" in output.getvalue()


def test_ignored_content_is_dirty_for_removal_preflight() -> None:
    fake = FakeGit(ignored_candidate=True)
    updater = GitUp(git=fake, cwd=Path("/repo"))

    assert updater._is_dirty(Path("/repo-wt"), include_ignored=True)
    assert not updater._is_dirty(Path("/repo-wt"), include_ignored=False)


def test_recursive_dirty_inspection_checks_initialized_submodule_status(
    tmp_path: Path,
) -> None:
    candidate_path = tmp_path / "candidate"
    fake = FakeGit(
        initialized_submodule=True,
        dirty_submodule=True,
        candidate_path=candidate_path,
    )
    fake.submodule_path.mkdir(parents=True)
    fake.submodule_path.joinpath(".git").write_text("gitdir: /fake\n")
    updater = GitUp(git=fake, cwd=Path("/repo"))

    assert updater._is_dirty(candidate_path, include_ignored=True)
    assert (
        (
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
            "--ignored=matching",
            "-z",
        ),
        fake.submodule_path,
        True,
        False,
    ) in fake.calls


def test_recursive_submodule_inspection_failure_retains_candidate() -> None:
    fake = FakeGit(fail_prefix=("ls-files", "--stage", "-z"))
    output = StringIO()

    code = GitUp(git=fake, cwd=Path("/repo"), stdout=output).run()

    assert code == 0
    assert not any(command[:2] == ("worktree", "remove") for command, *_ in fake.calls)
    assert "Retained worktree /repo-wt: uninspectable." in output.getvalue()


def test_final_candidate_preflight_precedes_first_worktree_removal() -> None:
    fake = FakeGit()
    candidate_path = Path("/repo-wt")

    assert GitUp(git=fake, cwd=Path("/repo")).run() == 0

    candidate_status = (
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
        "--ignored=matching",
        "-z",
    )
    candidate_gitlinks = ("ls-files", "--stage", "-z")
    expected_candidate_inspection = [
        (("rev-parse", "--verify", "HEAD^{commit}"), candidate_path),
        (("rev-parse", "--verify", "refs/heads/merged^{commit}"), candidate_path),
        (("merge-base", "--is-ancestor", SHA_A, SHA_B), candidate_path),
        (candidate_status, candidate_path),
        (candidate_gitlinks, candidate_path),
    ]
    candidate_calls = [
        (command, cwd) for command, cwd, *_rest in fake.calls if cwd == candidate_path
    ]

    assert candidate_calls == expected_candidate_inspection * 2
    assert candidate_calls.count((candidate_status, candidate_path)) == 2
    assert candidate_calls.count((candidate_gitlinks, candidate_path)) == 2

    candidate_call_indices = [
        index
        for index, (_command, cwd, *_rest) in enumerate(fake.calls)
        if cwd == candidate_path
    ]
    assert len(candidate_call_indices) == len(expected_candidate_inspection) * 2
    first_remove_index = next(
        index
        for index, (command, *_rest) in enumerate(fake.calls)
        if command[:2] == ("worktree", "remove")
    )

    final_revalidation_start = candidate_call_indices[
        len(expected_candidate_inspection)
    ]
    final_revalidation_end = candidate_call_indices[-1]
    assert final_revalidation_start < first_remove_index
    assert final_revalidation_end < first_remove_index


def test_final_recheck_retains_worktree_changed_after_preflight() -> None:
    fake = FakeGit(change_on_final_recheck=True)
    output = StringIO()

    code = GitUp(git=fake, cwd=Path("/repo"), stdout=output).run()

    assert code == 0
    assert not any(command[:2] == ("worktree", "remove") for command, *_ in fake.calls)
    assert "final recheck" in output.getvalue()


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


def test_repository_lock_is_atomic_and_released(tmp_path: Path) -> None:
    common_dir = tmp_path / "common"
    common_dir.mkdir()
    first = RepositoryLock(common_dir)
    first.acquire()
    try:
        assert first.path.read_text(encoding="utf-8").startswith("pid=")
        with pytest.raises(GitUpError, match="remove it after confirming"):
            RepositoryLock(common_dir).acquire()
    finally:
        first.release()

    second = RepositoryLock(common_dir)
    second.acquire()
    second.release()
    assert not second.path.exists()
