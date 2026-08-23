"""Command-line entry point for the ``git up`` Git extension."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .core import GitUp


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="git-up",
        description=(
            "Fast-forward origin's default branch and safely remove merged worktrees."
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``git up`` or print its metadata."""
    parser = _parser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the update and cleanup plan without changing the repository",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="show the installed version and exit",
    )
    arguments = list(argv) if argv is not None else sys.argv[1:]
    options = parser.parse_args(arguments)
    if options.version:
        print(f"git-up {__version__}")
        return 0
    return GitUp(cwd=Path.cwd()).run(dry_run=options.dry_run)
