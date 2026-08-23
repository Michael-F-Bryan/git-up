from importlib.metadata import version

import git_up


def test_runtime_version_matches_installed_distribution() -> None:
    assert git_up.__version__ == version("mfb-git-up")
