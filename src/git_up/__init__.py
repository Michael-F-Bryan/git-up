"""The mfb-git-up package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mfb-git-up")
except PackageNotFoundError:
    # Source checkouts without an installed distribution have no version.
    __version__ = "unknown"
