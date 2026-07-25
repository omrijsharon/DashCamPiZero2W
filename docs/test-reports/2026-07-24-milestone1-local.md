# Milestone 1 local foundation report

## Scope and authorization

- Date: 2026-07-24 local / 2026-07-23 UTC
- Scope: local Phase 0A repository foundation only
- Hardware access: none
- Raspberry Pi, provisioning, destructive storage, and Windows interoperability
  claims: not evaluated

## Environment

- Host: Microsoft Windows 11 Pro 10.0.26200, AMD64
- Local Python: 3.12.6
- Compatibility test interpreters: 3.11.15, 3.12.6, 3.13.14
- uv: 0.11.31
- Ruff: 0.16.0
- mypy: 1.20.2
- pytest: 9.1.1

## Validated commands and results

```text
uv sync --frozen --group dev
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest --junitxml=artifacts/pytest-py312.xml
uv run --isolated --python 3.11 --frozen pytest
uv run --isolated --python 3.13 --frozen pytest
uv build --no-sources
uv run --frozen dashcam-version --json
```

Results:

- Lockfile synchronized.
- Ruff formatting and lint checks passed.
- Strict mypy passed for `src` and `tests`.
- Nine tests passed on Python 3.11, 3.12, and 3.13.
- Both JSON Schemas passed Draft 2020-12 meta-schema validation and positive/
  negative document tests.
- Source distribution and wheel built successfully.
- Installed CLI reported version/build identity as valid JSON.
- The Python 3.12 CI-equivalent suite passed again from a fresh isolated source
  copy that excluded the working virtual environment, caches, build output, and
  artifacts.
- GitHub Actions YAML parsed with five expected jobs.

## Limitations

The repository has no initial Git commit (`HEAD`) yet, so a literal clean Git
checkout and hosted GitHub Actions run do not exist. The isolated source-copy run
validates reproducibility without changing repository history. Hosted CI should
be confirmed after the owner chooses to create and push the initial commit.

