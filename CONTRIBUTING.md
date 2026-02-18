# Contributing to Meshtastic Python

## Development resources

- [API Documentation](https://python.meshtastic.org/)
- [Meshtastic Python Development](https://meshtastic.org/docs/development/python/)
- [Building Meshtastic Python](https://meshtastic.org/docs/development/python/building/)
- [Using the Meshtastic Python Library](https://meshtastic.org/docs/development/python/library/)

## How to check your code (pytest/pylint) before a PR

- [Pre-requisites](https://meshtastic.org/docs/development/python/building/#pre-requisites)
- also execute `poetry install --all-extras --with dev,powermon` for all optional dependencies

### Quick check (recommended)

Run all CI checks locally with a single command:

```bash
make ci
```

This runs the exact same checks as CI (pylint, mypy, pytest with coverage).

### Manual checks

Alternatively, run each check individually:

```bash
poetry run pylint meshtastic examples/ --ignore-patterns ".*_pb2.pyi?$"
poetry run mypy meshtastic/
poetry run pytest --cov=meshtastic --cov-report=xml
```

### Using GitHub CI actions locally

- You need to have act installed. You can get it at https://nektosact.com/
- on Linux: `act -P ubuntu-latest=-self-hosted --matrix "python-version:3.12"`
- on Windows:
  - linux checks (linux docker): `act --matrix "python-version:3.12"`
  - Windows checks (Windows host): `act -P ubuntu-latest=-self-hosted --matrix "python-version:3.12"`

For more commands see [CI workflow](https://github.com/meshtastic/python/blob/master/.github/workflows/ci.yml)
