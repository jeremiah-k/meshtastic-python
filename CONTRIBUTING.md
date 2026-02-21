# Contributing to Meshtastic Python

## Development resources

- [API Documentation](https://python.meshtastic.org/)
- [Meshtastic Python Development](https://meshtastic.org/docs/development/python/)
- [Building Meshtastic Python](https://meshtastic.org/docs/development/python/building/)
- [Using the Meshtastic Python Library](https://meshtastic.org/docs/development/python/library/)

## API naming and compatibility policy

Use this policy for all code changes (especially AI-assisted refactors):

- Public API names should be `camelCase` (for example `sendText`, `sendData`).
- Internal helpers should be underscore-prefixed `snake_case` (for example `_send_packet`).
- Do not break existing public API names for compatibility.
- Symbols in internal subsystem modules (like `meshtastic/interfaces/ble/*`) are
  internal by default unless exposed through the primary package facade.

### BLE compatibility rule

The BLE surface has historical public `snake_case` names from
`master`/`meshtastic.ble_interface` (for example `find_device`,
`read_gatt_char`, `start_notify`). Those names are compatibility APIs and must
remain callable.

When modernizing BLE naming:

1. Keep historical `snake_case` methods callable.
2. Add `camelCase` aliases where needed for consistency.
3. Route both names to a single implementation (prefer internal
   underscore-prefixed helper methods).
4. Do not silently remove or hard-rename legacy methods.
5. Update tests/monkeypatch points if alias names are introduced.

## How to check your code (pytest/pylint) before a PR

- [Pre-requisites](https://meshtastic.org/docs/development/python/building/#pre-requisites)
- also execute `poetry install --all-extras --with dev,powermon` for all optional dependencies

### Quick check (recommended)

Run all CI checks locally with a single command:

```bash
make ci
```

This runs the same checks as CI (pylint, mypy, pytest with coverage).

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
  - Windows checks (Windows host): `act -P windows-latest=-self-hosted --matrix "python-version:3.12"`

For more commands see [CI workflow](.github/workflows/ci.yml)
