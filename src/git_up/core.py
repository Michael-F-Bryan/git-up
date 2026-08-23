"""Safe implementation of the ``git up`` workflow."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
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


def _decode(value: bytes) -> str:
    return os.fsdecode(value).strip("\r\n")


def _command_error(
    command: Sequence[str], cwd: Path | None, result: CommandResult
) -> GitUpError:
    detail = _decode(result.stderr) or "Git exited without an error message."
    location = f" in {cwd}" if cwd is not None else ""
    return GitUpError(f"Git command {' '.join(command)!r}{location} failed: {detail}")


def parse_worktree_list(output: bytes) -> list[Worktree]:
    """Parse ``git worktree list --porcelain -z`` without line assumptions."""
    records: list[Worktree] = []
    fields = [_decode(field) for field in output.split(b"\0") if field]
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

    index = 0
    while index < len(fields):
        field = fields[index].lstrip("\r\n")
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
        index += 1
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
    target: str,
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
        worktrees = self._worktrees(caller)
        primary = worktrees[0]
        if primary.bare:
            raise GitUpError(
                "the primary worktree is bare; a checked-out branch is required"
            )
        caller_worktree = next(
            (item for item in worktrees if _same_path(item.path, caller)), None
        )
        if caller_worktree is None:
            raise GitUpError(
                f"current repository root {caller} is not a listed worktree"
            )

        default_branch = self._default_branch(dry_run=dry_run)
        self._validate_primary_branch(primary, default_branch)
        primary_dirty = self._is_dirty(primary.path)
        if primary_dirty and not dry_run:
            raise GitUpError(
                f"primary worktree {primary.path} is dirty; refusing to sync"
            )

        if not dry_run:
            self._run_git(["fetch", "--prune", "origin"], cwd=self.cwd)
            print("Fetched and pruned origin.", file=self.stdout)
        target = self._remote_target(default_branch)

        if primary_dirty:
            print(
                f"Retained primary update: {primary.path} (dirty; update refused).",
                file=self.stdout,
            )
            return 1

        current_head = self._rev_parse("HEAD", primary.path)
        if not self._is_ancestor(current_head, target, primary.path):
            raise GitUpError(
                f"primary branch {default_branch!r} diverged from "
                f"origin/{default_branch}; "
                "refusing reset, merge, or rebase"
            )
        if current_head == target:
            print(
                f"Primary worktree {primary.path} is already up to date at {target}.",
                file=self.stdout,
            )
        elif dry_run:
            print(
                f"Would fast-forward primary worktree {primary.path} to {target}.",
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

        failures = self._cleanup(
            worktrees,
            primary=primary.path,
            caller=caller,
            target=target,
            dry_run=dry_run,
        )
        if dry_run:
            print("Dry run: no repository changes were made.", file=self.stdout)
        elif not failures:
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
        return Path(_decode(result.stdout)).resolve(strict=False)

    def _worktrees(self, cwd: Path) -> list[Worktree]:
        result = self._run_git(["worktree", "list", "--porcelain", "-z"], cwd=cwd)
        return parse_worktree_list(result.stdout)

    def _default_branch(self, *, dry_run: bool) -> str:
        if dry_run:
            origin = self._run_git(
                ["config", "--get", "remote.origin.url"], cwd=self.cwd, check=False
            )
            if origin.returncode:
                raise GitUpError("repository has no configured origin remote")
            result = self._run_git(
                ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
                cwd=self.cwd,
                check=False,
            )
            if result.returncode:
                raise GitUpError(
                    "dry run requires local refs/remotes/origin/HEAD; "
                    "run a normal git up first or fetch origin"
                )
            prefix = "refs/remotes/origin/"
            value = _decode(result.stdout)
            if not value.startswith(prefix) or not value.removeprefix(prefix):
                raise GitUpError("local origin/HEAD is not a usable default branch ref")
            return value.removeprefix(prefix)

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
            detail = _decode(result.stderr)
            suffix = f": {detail}" if detail else ""
            raise GitUpError(
                f"origin/{default_branch} is unavailable after fetching origin{suffix}"
            )
        target = _decode(result.stdout)
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", target):
            raise GitUpError(
                f"origin/{default_branch} is unavailable after fetching origin"
            )
        return target

    def _rev_parse(self, revision: str, cwd: Path) -> str:
        result = self._run_git(
            ["rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=cwd
        )
        value = _decode(result.stdout)
        if not value:
            raise GitUpError(f"Git returned no commit for {revision!r} in {cwd}")
        return value

    def _is_dirty(self, cwd: Path) -> bool:
        result = self._run_git(
            [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
            cwd=cwd,
        )
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

    def _cleanup(
        self,
        worktrees: list[Worktree],
        *,
        primary: Path,
        caller: Path,
        target: str,
        dry_run: bool,
    ) -> bool:
        failures = False
        for worktree in worktrees:
            reason: str | None
            inspectable = True
            dirty = False
            merged = False
            if worktree.detached or worktree.branch is None:
                reason = "detached"
            elif worktree.locked:
                reason = "locked"
            elif worktree.prunable:
                reason = "prunable metadata"
            else:
                try:
                    dirty = self._is_dirty(worktree.path)
                    candidate_head = self._rev_parse("HEAD", worktree.path)
                    merged = self._is_ancestor(candidate_head, target, worktree.path)
                except GitUpError:
                    inspectable = False
                reason = classify_worktree(
                    worktree,
                    primary=primary,
                    caller=caller,
                    target=target,
                    dirty=dirty,
                    inspectable=inspectable,
                    merged=merged,
                )
            if reason is not None:
                print(f"Retained worktree {worktree.path}: {reason}.", file=self.stdout)
                continue
            if dry_run:
                print(f"Would remove worktree {worktree.path}.", file=self.stdout)
                continue
            try:
                self._deinit_submodules(worktree.path)
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
            if worktree.branch is None:
                continue
            try:
                remaining = self._worktrees(caller)
                branch_in_use = any(
                    item.branch == worktree.branch for item in remaining
                )
                if branch_in_use:
                    print(
                        f"Retained branch {worktree.branch}: another worktree uses it.",
                        file=self.stdout,
                    )
                else:
                    self._run_git(["branch", "-d", "--", worktree.branch], cwd=caller)
                    print(f"Deleted merged branch {worktree.branch}.", file=self.stdout)
            except GitUpError as error:
                failures = True
                print(
                    f"Failed to delete branch {worktree.branch} after removing "
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

    def _deinit_submodules(self, cwd: Path) -> None:
        result = self._run_git(["submodule", "status", "--recursive"], cwd=cwd)
        paths: list[str] = []
        for line in _decode(result.stdout).splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                paths.append(parts[1].split(" (", maxsplit=1)[0])
        if paths:
            paths.sort(key=lambda path: path.count("/"), reverse=True)
            self._run_git(["submodule", "deinit", "--force", "--", *paths], cwd=cwd)
