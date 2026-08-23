from importlib.metadata import entry_points

from git_up.cli import main


def test_console_script_is_registered_and_version_is_useful() -> None:
    scripts = [
        entry_point
        for entry_point in entry_points(group="console_scripts")
        if entry_point.name == "git-up"
    ]

    assert len(scripts) == 1
    assert scripts[0].value == "git_up.cli:main"
    assert scripts[0].load() is main
    assert main(["--version"]) == 0
