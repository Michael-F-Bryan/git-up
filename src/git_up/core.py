"""Safe implementation of the ``git up`` workflow."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, TextIO


class GitUpError(Exception):
    """A user-facing failure with enough context to act on it."""


@dataclass(frozen=True)
class CommandResult:
    """The small part of a Git command result used by the workflow."""

    returncode: int
    stdout: bytes
    stderr: bytes


class GitExecutor(Protocol):
    """Command boundary kept injectable for focused unit tests."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        read_only: bool = False,
    ) -> CommandResult: ...


class SubprocessGit:
    """Run the real Git executable without invoking a shell."""

    def __init__(self, executable: str = "git") -> None:
        self.executable = executable

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        read_only: bool = False,
    ) -> CommandResult:
        command = [self.executable, *args]
        environment = None
        if read_only:
            environment = os.environ.copy()
            environment["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                capture_output=True,
                check=False,
            )
        except OSError as error:
            raise GitUpError(
                f"unable to execute Git in {cwd or Path.cwd()}: {error}"
            ) from error
        result = CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode:
            raise _command_error(command, cwd, result)
        return result


@dataclass(frozen=True)
class Worktree:
    """The inspectable facts Git reports for one worktree."""

    path: Path
    head: str
    branch: str | None
    detached: bool = False
    locked: bool = False
    lock_reason: str | None = None
    prunable: bool = False
    prune_reason: str | None = None
    bare: bool = False


@dataclass(frozen=True)
class WorktreeInspection:
    """One complete, point-in-time worktree preflight result."""

    worktree: Worktree
    head_oid: str
    ref_oid: str | None
    dirty: bool
    merged: bool
    reason: str | None


class RepositoryLock:
    """A cooperative, atomic lock for concurrent ``git up`` processes."""

    filename = "git-up.lock"

    def __init__(self, common_dir: Path) -> None:
        self.path = common_dir.resolve(strict=False) / self.filename

    def acquire(self) -> None:
        payload = (
            f"pid={os.getpid()}\n"
            f"cwd={Path.cwd()}\n"
            "This is a cooperative git up lock; inspect before removing it.\n"
        ).encode()
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError as error:
            raise GitUpError(
                f"repository lock {self.path} already exists; remove it after "
                "confirming no git up process is running"
            ) from error
        except OSError as error:
            raise GitUpError(
                f"unable to acquire repository lock {self.path}: {error}"
            ) from error
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
        except OSError as error:
            self.path.unlink(missing_ok=True)
            raise GitUpError(
                f"unable to write repository lock {self.path}: {error}"
            ) from error

    def release(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as error:
            raise GitUpError(
                f"unable to release repository lock {self.path}: {error}"
            ) from error


def _decode(value: bytes) -> str:
    """Decode bytes without stripping data that is not protocol framing."""
    return os.fsdecode(value)


def _text(value: bytes) -> str:
    """Decode a line-oriented Git result and remove only line framing."""
    return _decode(value).strip("\r\n")


def _command_error(
    command: Sequence[str], cwd: Path | None, result: CommandResult
) -> GitUpError:
    detail = _text(result.stderr) or "Git exited without an error message."
    location = f" in {cwd}" if cwd is not None else ""
    return GitUpError(f"Git command {' '.join(command)!r}{location} failed: {detail}")


def parse_worktree_list(output: bytes) -> list[Worktree]:
    """Parse ``git worktree list --porcelain -z`` without line assumptions."""
    records: list[Worktree] = []
    current: dict[str, object] | None = None

    def finish() -> None:
        if current is None:
            return
        path = current.get("path")
        head = current.get("head")
        if not isinstance(path, str) or not isinstance(head, str):
            raise GitUpError("Git returned an incomplete worktree record.")
        branch = current.get("branch")
        lock_reason = current.get("lock_reason")
        prune_reason = current.get("prune_reason")
        records.append(
            Worktree(
                path=Path(path),
                head=head,
                branch=branch if isinstance(branch, str) else None,
                detached=bool(current.get("detached", False)),
                locked=bool(current.get("locked", False)),
                lock_reason=lock_reason if isinstance(lock_reason, str) else None,
                prunable=bool(current.get("prunable", False)),
                prune_reason=prune_reason if isinstance(prune_reason, str) else None,
                bare=bool(current.get("bare", False)),
            )
        )

    for raw_field in output.split(b"\0"):
        if not raw_field:
            continue
        field = _decode(raw_field)
        # Git uses a leading LF as the record separator in this porcelain form.
        # Remove that separator only when it precedes a protocol key, never from
        # a path or reason value.
        if field.startswith("\n") and field[1:].split(" ", 1)[0] in {
            "worktree",
            "HEAD",
            "branch",
            "detached",
            "bare",
            "locked",
            "prunable",
        }:
            field = field[1:]
        key, separator, value = field.partition(" ")
        if key == "worktree":
            finish()
            current = {"path": value if separator else ""}
        elif current is not None:
            if key == "HEAD":
                current["head"] = value
            elif key == "branch":
                prefix = "refs/heads/"
                if not value.startswith(prefix):
                    raise GitUpError(f"Unexpected worktree branch ref {value!r}.")
                current["branch"] = value.removeprefix(prefix)
            elif key == "detached":
                current["detached"] = True
            elif key == "bare":
                current["bare"] = True
            elif key == "locked":
                current["locked"] = True
                current["lock_reason"] = value if separator else None
            elif key == "prunable":
                current["prunable"] = True
                current["prune_reason"] = value if separator else None
    finish()
    if not records:
        raise GitUpError("Git reported no worktrees; are you inside a repository?")
    return records


def default_branch_from_ls_remote(output: bytes) -> str:
    """Extract the remote's symbolic HEAD target from ``ls-remote`` output."""
    for raw_line in output.splitlines():
        line = _decode(raw_line)
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            branch = line.removeprefix("ref: refs/heads/").removesuffix("\tHEAD")
            if branch:
                return branch
    raise GitUpError("origin did not advertise a default branch through HEAD.")


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


def classify_worktree(
    worktree: Worktree,
    *,
    primary: Path,
    caller: Path,
    dirty: bool,
    inspectable: bool,
    merged: bool,
) -> str | None:
    """Return a defensive retention reason, or ``None`` for a removal candidate."""
    if _same_path(worktree.path, caller):
        return "current caller"
    if _same_path(worktree.path, primary):
        return "primary"
    if worktree.detached or worktree.branch is None:
        return "detached"
    if worktree.locked:
        return "locked"
    if worktree.prunable:
        return "prunable metadata"
    if not inspectable:
        return "uninspectable"
    if dirty:
        return "dirty"
    if not merged:
        return "unmerged"
    return None


class GitUp:
    """Plan and, unless requested otherwise, execute a safe repository update."""

    def __init__(
        self,
        *,
        git: GitExecutor | None = None,
        cwd: Path | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        self.git = git or SubprocessGit()
        self.cwd = (cwd or Path.cwd()).resolve()
        self.stdout = stdout or sys.stdout
        self.read_only = False

    def run(self, *, dry_run: bool = False) -> int:
        self.read_only = dry_run
        try:
            return self._run(dry_run=dry_run)
        except GitUpError as error:
            print(f"git-up: error: {error}", file=sys.stderr)
            return 1

    def _run(self, *, dry_run: bool) -> int:
        caller = self._repository_root(self.cwd)
        common_dir = self._repository_common_dir(self.cwd)
        if dry_run:
            return self._run_locked(caller, dry_run=True)

        lock = RepositoryLock(common_dir)
        lock.acquire()
        try:
            return self._run_locked(caller, dry_run=False)
        finally:
            lock.release()

    def _run_locked(self, caller: Path, *, dry_run: bool) -> int:
        worktrees = self._worktrees(caller)
        primary = worktrees[0]
        if primary.bare:
            raise GitUpError(
                "the primary worktree is bare; a checked-out branch is required"
            )
        if not any(_same_path(item.path, caller) for item in worktrees):
            raise GitUpError(
                f"current repository root {caller} is not a listed worktree"
            )

        default_branch = self._default_branch()
        self._validate_primary_branch(primary, default_branch)
        primary_dirty = self._is_dirty(primary.path, include_ignored=False)
        if primary_dirty and not dry_run:
            raise GitUpError(
                f"primary worktree {primary.path} is dirty; refusing to sync"
            )
        if primary_dirty:
            print(
                f"Retained primary update: {primary.path} (dirty; update refused).",
                file=self.stdout,
            )
            return 1

        if not dry_run:
            self._run_git(["fetch", "--prune", "origin"], cwd=self.cwd)
            print("Fetched and pruned origin.", file=self.stdout)
        target = self._remote_target(default_branch)
        plan = self._build_plan(
            worktrees,
            primary=primary.path,
            caller=caller,
            target=target,
        )
        primary_item = next(
            item for item in plan if _same_path(item.worktree.path, primary.path)
        )

        if dry_run:
            if primary_item.head_oid == target:
                print(
                    f"Primary worktree {primary.path} is already up to date "
                    f"at {target}.",
                    file=self.stdout,
                )
            else:
                print(
                    f"Would fast-forward primary worktree {primary.path} to {target}.",
                    file=self.stdout,
                )
            self._cleanup(plan, caller=caller, target=target, dry_run=True)
            print("Dry run: no repository changes were made.", file=self.stdout)
            return 0

        self._revalidate(
            primary_item,
            caller=caller,
            target=target,
            include_ignored=False,
        )
        if primary_item.head_oid == target:
            print(
                f"Primary worktree {primary.path} is already up to date at {target}.",
                file=self.stdout,
            )
        else:
            self._run_git(
                ["merge", "--ff-only", f"refs/remotes/origin/{default_branch}"],
                cwd=primary.path,
            )
            print(
                f"Updated primary worktree {primary.path} to {target}.",
                file=self.stdout,
            )

        failures = self._cleanup(plan, caller=caller, target=target, dry_run=False)
        if not failures:
            print("Pruned stale unlocked worktree metadata.", file=self.stdout)
        return 1 if failures else 0

    def _run_git(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> CommandResult:
        return self.git.run(
            args,
            cwd=cwd,
            check=check,
            read_only=self.read_only,
        )

    def _repository_root(self, cwd: Path) -> Path:
        result = self._run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
        return Path(_text(result.stdout)).resolve(strict=False)

    def _repository_common_dir(self, cwd: Path) -> Path:
        result = self._run_git(["rev-parse", "--git-common-dir"], cwd=cwd)
        value = Path(_text(result.stdout))
        if not value.is_absolute():
            value = cwd / value
        return value.resolve(strict=False)

    def _worktrees(self, cwd: Path) -> list[Worktree]:
        result = self._run_git(["worktree", "list", "--porcelain", "-z"], cwd=cwd)
        return parse_worktree_list(result.stdout)

    def _default_branch(self) -> str:
        result = self._run_git(
            ["ls-remote", "--symref", "origin", "HEAD"], cwd=self.cwd
        )
        return default_branch_from_ls_remote(result.stdout)

    def _validate_primary_branch(self, primary: Worktree, default_branch: str) -> None:
        if primary.branch != default_branch:
            actual = primary.branch or "(detached)"
            raise GitUpError(
                f"primary worktree is on {actual!r}, not origin/{default_branch!s}; "
                "refusing to update it"
            )

    def _remote_target(self, default_branch: str) -> str:
        result = self._run_git(
            [
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{default_branch}^{{commit}}",
            ],
            cwd=self.cwd,
            check=False,
        )
        if result.returncode:
            detail = _text(result.stderr)
            suffix = f": {detail}" if detail else ""
            raise GitUpError(
                f"origin/{default_branch} is unavailable after fetching origin{suffix}"
            )
        target = _text(result.stdout)
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", target):
            raise GitUpError(
                f"origin/{default_branch} is unavailable after fetching origin"
            )
        return target

    def _rev_parse(self, revision: str, cwd: Path) -> str:
        result = self._run_git(
            ["rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=cwd
        )
        value = _text(result.stdout)
        if not value:
            raise GitUpError(f"Git returned no commit for {revision!r} in {cwd}")
        return value

    def _is_dirty(self, cwd: Path, *, include_ignored: bool) -> bool:
        args = [
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ]
        if include_ignored:
            args.append("--ignored=matching")
        result = self._run_git(args, cwd=cwd)
        return bool(result.stdout)

    def _is_ancestor(self, ancestor: str, descendant: str, cwd: Path) -> bool:
        result = self._run_git(
            ["merge-base", "--is-ancestor", ancestor, descendant],
            cwd=cwd,
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise _command_error(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd,
            result,
        )

    def _inspect(
        self,
        worktree: Worktree,
        *,
        target: str,
        include_ignored: bool,
        reason: str | None = None,
    ) -> WorktreeInspection:
        head_oid = self._rev_parse("HEAD", worktree.path)
        if head_oid != worktree.head:
            raise GitUpError(f"worktree {worktree.path} changed during preflight")
        ref_oid: str | None = None
        merged = False
        if worktree.branch is not None:
            ref_oid = self._rev_parse(f"refs/heads/{worktree.branch}", worktree.path)
            if ref_oid != head_oid:
                raise GitUpError(f"branch {worktree.branch} changed during preflight")
            merged = self._is_ancestor(head_oid, target, worktree.path)
        dirty = self._is_dirty(worktree.path, include_ignored=include_ignored)
        return WorktreeInspection(worktree, head_oid, ref_oid, dirty, merged, reason)

    def _build_plan(
        self,
        worktrees: list[Worktree],
        *,
        primary: Path,
        caller: Path,
        target: str,
    ) -> list[WorktreeInspection]:
        plan: list[WorktreeInspection] = []
        for worktree in worktrees:
            if _same_path(worktree.path, primary):
                inspection = self._inspect(
                    worktree,
                    target=target,
                    include_ignored=False,
                    reason="primary",
                )
            else:
                reason = classify_worktree(
                    worktree,
                    primary=primary,
                    caller=caller,
                    dirty=False,
                    inspectable=True,
                    merged=True,
                )
                if reason is not None:
                    plan.append(self._retained(worktree, reason))
                    continue
                try:
                    inspected = self._inspect(
                        worktree,
                        target=target,
                        include_ignored=True,
                    )
                except GitUpError:
                    inspection = self._retained(worktree, "uninspectable")
                else:
                    inspection = replace(
                        inspected,
                        reason=classify_worktree(
                            worktree,
                            primary=primary,
                            caller=caller,
                            dirty=inspected.dirty,
                            inspectable=True,
                            merged=inspected.merged,
                        ),
                    )
            plan.append(inspection)
        return plan

    @staticmethod
    def _retained(worktree: Worktree, reason: str) -> WorktreeInspection:
        return WorktreeInspection(worktree, worktree.head, None, False, False, reason)

    def _revalidate(
        self,
        item: WorktreeInspection,
        *,
        caller: Path,
        target: str,
        include_ignored: bool,
    ) -> WorktreeInspection:
        listed = next(
            (
                worktree
                for worktree in self._worktrees(caller)
                if _same_path(worktree.path, item.worktree.path)
            ),
            None,
        )
        if listed is None or listed != item.worktree:
            raise GitUpError(
                f"worktree {item.worktree.path} changed during final recheck"
            )
        current = self._inspect(
            listed,
            target=target,
            include_ignored=include_ignored,
            reason=item.reason,
        )
        if current != item:
            raise GitUpError(
                f"worktree {item.worktree.path} changed during final recheck"
            )
        return current

    def _cleanup(
        self,
        plan: list[WorktreeInspection],
        *,
        caller: Path,
        target: str,
        dry_run: bool,
    ) -> bool:
        failures = False
        for item in plan:
            worktree = item.worktree
            reason = item.reason
            if reason is not None:
                print(f"Retained worktree {worktree.path}: {reason}.", file=self.stdout)
                continue
            if dry_run:
                print(f"Would remove worktree {worktree.path}.", file=self.stdout)
                continue
            try:
                self._revalidate(
                    item,
                    caller=caller,
                    target=target,
                    include_ignored=True,
                )
            except GitUpError as error:
                print(f"Retained worktree {worktree.path}: {error}.", file=self.stdout)
                continue
            try:
                self._run_git(
                    ["worktree", "remove", "--force", "--", str(worktree.path)]
                )
            except GitUpError as error:
                failures = True
                print(
                    f"Failed to remove worktree {worktree.path}: {error}.",
                    file=self.stdout,
                )
                continue
            print(f"Removed worktree {worktree.path}.", file=self.stdout)
            branch = worktree.branch
            expected_oid = item.ref_oid
            if branch is None or expected_oid is None:
                continue
            try:
                remaining = self._worktrees(caller)
                if any(candidate.branch == branch for candidate in remaining):
                    print(
                        f"Retained branch {branch}: another worktree uses it.",
                        file=self.stdout,
                    )
                else:
                    self._run_git(
                        ["update-ref", "-d", f"refs/heads/{branch}", expected_oid],
                        cwd=caller,
                    )
                    print(f"Deleted merged branch {branch}.", file=self.stdout)
            except GitUpError as error:
                failures = True
                print(
                    f"Failed to delete branch {branch} after removing "
                    f"{worktree.path}: {error}.",
                    file=self.stdout,
                )
        if not dry_run:
            try:
                self._run_git(["worktree", "prune"], cwd=caller)
            except GitUpError as error:
                failures = True
                print(
                    f"Failed to prune stale worktree metadata: {error}.",
                    file=self.stdout,
                )
        return failures
