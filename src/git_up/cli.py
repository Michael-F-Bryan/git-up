"""Command-line entry point for the Git extension placeholder."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="git-up",
        description=(
            "A future Git workflow helper. The workflow is not implemented yet."
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Print placeholder help or version information without running a workflow."""
    parser = _parser()
    parser.add_argument(
        "--version",
        action="store_true",
        help="show the installed version and exit",
    )
    arguments = list(argv) if argv is not None else sys.argv[1:]

    if not arguments:
        parser.print_help()
        return 0

    options = parser.parse_args(arguments)
    if options.version:
        print(f"git-up {__version__}")
    return 0
