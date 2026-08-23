"""Hermetic real-Git fixtures for the ``git up`` integration contract."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class GitCommandResult:
    """The observable result of one real Git or ``git up`` invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


@dataclass
class GitRepository:
    """A tiny repository with an explicit, hermetic command environment."""

    path: Path
    env: dict[str, str]

    def run(
        self,
        *arguments: str,
        check: bool = True,
        extra_env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> GitCommandResult:
        command = ["git", *arguments]
        environment = self.env | (extra_env or {})
        completed = subprocess.run(
            command,
            cwd=cwd or self.path,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        result = GitCommandResult(
            completed.returncode, completed.stdout, completed.stderr
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"Git command failed ({result.returncode}): {' '.join(command)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def git_up(self, *arguments: str, check: bool = False) -> GitCommandResult:
        return self.run("up", *arguments, check=check)

    def write(self, relative_path: str, content: str) -> Path:
        destination = self.path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return destination

    def commit(self, message: str, *, relative_path: str = "README.md") -> str:
        self.run("add", "--", relative_path)
        self.run("commit", "--no-gpg-sign", "-m", message)
        return self.run("rev-parse", "HEAD", check=True).stdout.strip()

    def branch_names(self) -> set[str]:
        output = self.run(
            "for-each-ref", "--format=%(refname:short)", "refs/heads"
        ).stdout
        return {line for line in output.splitlines() if line}

    def remote_branch_names(self) -> set[str]:
        output = self.run(
            "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"
        ).stdout
        return {line for line in output.splitlines() if line}

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").stdout.strip()

    def current_branch(self) -> str | None:
        result = self.run("symbolic-ref", "--short", "-q", "HEAD", check=False)
        return result.stdout.strip() or None

    def worktree_porcelain(self) -> str:
        return self.run("worktree", "list", "--porcelain").stdout

    def git_dir(self) -> Path:
        result = self.run("rev-parse", "--git-dir")
        git_dir = Path(result.stdout.strip())
        return git_dir if git_dir.is_absolute() else self.path / git_dir


@dataclass(frozen=True)
class RepositorySnapshot:
    """The safety-critical state surfaces observed by integration tests."""

    head: str
    indices: tuple[tuple[str, bytes], ...]
    status: str
    files: tuple[tuple[str, bytes], ...]
    refs: str
    reflogs: tuple[tuple[str, bytes], ...]
    config: bytes
    fetch_head: bytes | None
    orig_head: bytes | None
    worktrees: str
    lock_files: tuple[tuple[str, bytes], ...]
    candidate_state: tuple[tuple[str, str | None, bool], ...]


def _tree_files(root: Path) -> tuple[tuple[str, bytes], ...]:
    """Capture checkout bytes while excluding internal Git object databases."""
    if not root.exists():
        return ()
    files: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git" and path.is_dir():
            continue
        if relative.parts and relative.parts[0] == ".git" and path.parent != root:
            continue
        if path.is_file():
            files.append((str(relative), path.read_bytes()))
    return tuple(files)


def snapshot_repository(
    repository: GitRepository,
    *,
    candidates: Iterable[GitRepository] = (),
    additional_trees: Iterable[GitRepository] = (),
) -> RepositorySnapshot:
    """Capture Git metadata plus every byte in the requested checkout trees."""
    git_dir = repository.git_dir()
    candidate_list = tuple(candidates)
    trees = (repository, *candidate_list, *tuple(additional_trees))
    files = tuple(
        sorted(
            (
                (f"{tree.path}:{relative}", content)
                for tree in trees
                for relative, content in _tree_files(tree.path)
            ),
            key=lambda item: item[0],
        )
    )
    statuses = []
    for tree in trees:
        if not tree.path.exists():
            statuses.append(f"{tree.path}\0")
            continue
        status = tree.run(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=none",
            extra_env={"GIT_OPTIONAL_LOCKS": "0"},
        ).stdout
        statuses.append(f"{tree.path}\0{status}")
    status = "".join(statuses)
    indices = []
    for tree in trees:
        if not tree.path.exists():
            continue
        index = tree.run(
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "index",
            extra_env={"GIT_OPTIONAL_LOCKS": "0"},
        ).stdout.strip()
        indices.append((str(tree.path), Path(index).read_bytes()))

    def byte_files(
        directory: Path, suffix: str | None = None
    ) -> tuple[tuple[str, bytes], ...]:
        if not directory.exists():
            return ()
        return tuple(
            (str(path.relative_to(directory)), path.read_bytes())
            for path in sorted(directory.rglob("*"))
            if path.is_file() and (suffix is None or path.name.endswith(suffix))
        )

    lock_files = byte_files(git_dir, ".lock")
    reflogs = byte_files(git_dir / "logs")
    candidate_state = tuple(
        (
            str(tree.path),
            tree.current_branch() if tree.path.exists() else None,
            tree.path.exists(),
        )
        for tree in candidate_list
    )
    return RepositorySnapshot(
        head=repository.head(),
        indices=tuple(indices),
        status=status,
        files=files,
        refs=repository.run("show-ref").stdout,
        reflogs=reflogs,
        config=(git_dir / "config").read_bytes(),
        fetch_head=(
            (git_dir / "FETCH_HEAD").read_bytes()
            if (git_dir / "FETCH_HEAD").exists()
            else None
        ),
        orig_head=(
            (git_dir / "ORIG_HEAD").read_bytes()
            if (git_dir / "ORIG_HEAD").exists()
            else None
        ),
        worktrees=repository.run(
            "worktree",
            "list",
            "--porcelain",
            "-z",
            extra_env={"GIT_OPTIONAL_LOCKS": "0"},
        ).stdout,
        lock_files=lock_files,
        candidate_state=candidate_state,
    )


@dataclass
class GitSandbox:
    """Factory for isolated repositories and local bare remotes."""

    root: Path
    env: dict[str, str]

    def repository(self, name: str = "repo", *, branch: str = "main") -> GitRepository:
        path = self.root / name
        path.mkdir(parents=True)
        repository = GitRepository(path, self.env.copy())
        repository.run("init", "--initial-branch", branch)
        repository.run("config", "user.name", "Integration Test")
        repository.run("config", "user.email", "integration@example.test")
        repository.write("README.md", "initial\n")
        repository.commit("initial commit")
        return repository

    def bare_remote(
        self, name: str = "origin", *, branch: str = "main"
    ) -> GitRepository:
        path = self.root / name
        path.mkdir(parents=True)
        repository = GitRepository(path, self.env.copy())
        repository.run("init", "--bare", "--initial-branch", branch)
        return repository

    def clone(
        self,
        remote: GitRepository,
        name: str = "clone",
        *,
        branch: str | None = None,
    ) -> GitRepository:
        path = self.root / name
        arguments = ["clone"]
        if branch is not None:
            arguments.extend(["--branch", branch])
        arguments.extend([str(remote.path), str(path)])
        GitRepository(self.root, self.env.copy()).run(*arguments)
        repository = GitRepository(path, self.env.copy())
        repository.run("config", "user.name", "Integration Test")
        repository.run("config", "user.email", "integration@example.test")
        return repository

    def link_remote(
        self,
        repository: GitRepository,
        remote: GitRepository,
        *,
        branch: str | None = None,
    ) -> None:
        branch = branch or repository.current_branch()
        if branch is None:
            raise AssertionError("cannot link a remote from a detached repository")
        repository.run("remote", "add", "origin", str(remote.path))
        repository.run("push", "--set-upstream", "origin", branch)
        repository.run("remote", "set-head", "origin", "-a")

    def linked_worktree(
        self,
        repository: GitRepository,
        name: str,
        branch: str,
        *,
        start_point: str = "HEAD",
    ) -> GitRepository:
        path = self.root / name
        repository.run("worktree", "add", "-b", branch, str(path), start_point)
        linked = GitRepository(path, self.env.copy())
        linked.run("config", "user.name", "Integration Test")
        linked.run("config", "user.email", "integration@example.test")
        return linked

    def detached_worktree(
        self, repository: GitRepository, name: str, *, commit: str = "HEAD"
    ) -> GitRepository:
        path = self.root / name
        repository.run("worktree", "add", "--detach", str(path), commit)
        return GitRepository(path, self.env.copy())

    def make_submodule(self, name: str = "submodule") -> GitRepository:
        return self.repository(name)

    def initialise_submodule(
        self,
        repository: GitRepository,
        source: GitRepository,
        destination: str = "vendor/submodule",
    ) -> None:
        # Keep file transport opt-in and scoped to this one Git command.
        repository.run(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(source.path),
            destination,
        )
        repository.commit("add submodule", relative_path=destination)


def _make_git_up_dispatcher(dispatch_directory: Path) -> None:
    dispatcher = dispatch_directory / "git-up"
    dispatch_command = (
        f"exec {sys.executable!s} -c "
        "'from git_up.cli import main; raise SystemExit(main())' \"$@\"\n"
    )
    dispatcher.write_text(
        "#!/bin/sh\n" + dispatch_command,
        encoding="utf-8",
    )
    dispatcher.chmod(
        dispatcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )


@pytest.fixture
def git_sandbox(tmp_path: Path) -> Iterable[GitSandbox]:
    """Provide isolated HOME/XDG/config state and a real ``git up`` dispatcher."""

    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    dispatch = tmp_path / "bin"
    home.mkdir()
    xdg.mkdir()
    dispatch.mkdir()
    _make_git_up_dispatcher(dispatch)

    source_root = Path(__file__).resolve().parents[2] / "src"
    environment = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_EDITOR": ":",
        "GIT_SEQUENCE_EDITOR": ":",
        "GIT_PAGER": "cat",
        "GIT_MERGE_AUTOEDIT": "no",
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONPATH": str(source_root),
        "PATH": os.pathsep.join([str(dispatch), os.environ["PATH"]]),
    }
    for variable in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_OBJECT_DIRECTORY_RELATIVE",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_PREFIX",
        "GIT_ALLOW_PROTOCOL",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    ):
        environment.pop(variable, None)
    yield GitSandbox(tmp_path, environment)


def assert_path_is_absent(path: Path) -> None:
    """Assert absence without conflating a missing worktree with an empty one."""

    assert not path.exists(), f"expected path to be removed: {path}"


def assert_path_is_present(path: Path) -> None:
    """Assert that a retained worktree still has a usable checkout."""

    assert path.is_dir(), f"expected retained worktree directory: {path}"


def remote_default(repository: GitRepository) -> str:
    """Return the symbolic default branch advertised by ``origin``."""

    return (
        repository.run("symbolic-ref", "refs/remotes/origin/HEAD")
        .stdout.strip()
        .split("/", maxsplit=3)[-1]
    )
