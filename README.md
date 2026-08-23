# mfb-git-up

A Python Git extension foundation for a future `git up` workflow.

The workflow is intentionally not implemented yet. The placeholder only exposes
help and version information and does not modify repositories.

## Install

```console
uv tool install .
git up --help
git up --version
```

## Development

```console
uv sync
git-up --help
uv run pytest
uv run ruff check .
uv run mypy
```

The project uses a `src/` layout, standard-library runtime code, and a small
smoke test for the packaged console-script entry point.
