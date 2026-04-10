# Contributing to mtjk (Meshtastic Python Fork)

## Development resources

- Fork repository: <https://github.com/jeremiah-k/meshtastic-python>
- Fork issue tracker: <https://github.com/jeremiah-k/meshtastic-python/issues>
- Upstream technical docs (still useful reference):
  - [API Documentation](https://python.meshtastic.org/)
  - [Meshtastic Python Development](https://meshtastic.org/docs/development/python/)
  - [Building Meshtastic Python](https://meshtastic.org/docs/development/python/building/)
  - [Using the Meshtastic Python Library](https://meshtastic.org/docs/development/python/library/)

## Python and typing baseline

- Runtime baseline is Python 3.10+ (see `pyproject.toml`: `python = "^3.10,<3.15"`).
- Use PEP 604 unions (`X | None`, `A | B`) and built-in generics
  (`dict[K, V]`, `list[T]`, `tuple[T, ...]`) for new and edited annotations.
- Do not churn code with typing-only mass rewrites; normalize typing style only
  in areas already being edited.
- If your LSP/type checker suggests replacing `|` with `Optional`/`Union`,
  fix the tool's interpreter/version configuration first (Poetry-managed env),
  rather than rewriting annotations for legacy pre-3.10 compatibility.
- Do not require contributors to manually create/activate a venv; use
  `poetry install ...` and run tools via `poetry run ...`.

## Docstring style

- The linted docstring convention is NumPy style (Ruff pydocstyle).
- Prefer NumPy-style docstrings for new and edited docstrings.
- Avoid mass docstring rewrites unrelated to the code you are changing.

## API naming and compatibility policy

Use this policy for all code changes (especially AI-assisted refactors):

- Canonical compatibility/deprecation inventory is maintained in
  `COMPATIBILITY.md`.
- New public API names should prefer `camelCase` (for example `sendText`,
  `sendData`).
- Existing public compatibility names must remain callable, including legacy BLE
  `snake_case` names documented in `COMPATIBILITY.md`.
- Internal helpers should be underscore-prefixed `snake_case` (for example
  `_send_packet`).
- Do not break existing public API names for compatibility.
- Symbols in internal subsystem modules (like `meshtastic/interfaces/ble/*`) are
  internal by default unless exposed through the primary package facade.
- AI-assisted refactors must not auto-rename BLE compatibility symbols or
  remove compatibility aliases unless maintainers explicitly request it.

### BLE compatibility rule

The BLE surface has historical public `snake_case` names from the
pre-refactor `meshtastic.ble_interface` API (for example `find_device`,
`read_gatt_char`, `start_notify`). Those names are compatibility APIs and must
remain callable.

When modernizing BLE naming:

1. Keep historical `snake_case` methods callable.
2. Keep only the currently approved BLE camelCase promotions callable:
   `findDevice`, `isConnected`, and `stopNotify`.
3. Route compatibility names to a single implementation (prefer internal
   underscore-prefixed helper methods).
4. Do not add new BLE aliases unless explicitly requested by maintainers.
5. Do not silently remove or hard-rename legacy methods.
6. Update tests/monkeypatch points if alias names are introduced.

#### Historical BLE compatibility baseline

Use this pinned baseline for BLE compatibility decisions:

- Tag: `2.7.7`
- Commit: `b26d80f1866ffa765467e5cb7688c59dee7f2bb2`
- Baseline file: `meshtastic/ble_interface.py`

Historical required BLE wrappers and warning policy are tracked in
`COMPATIBILITY.md` under **BLE Historical Baseline (2.7.7)**.

## How to check your code (pytest/pylint/ruff/mypy) before a PR

- [Pre-requisites](https://meshtastic.org/docs/development/python/building/#pre-requisites)
- also execute `poetry install --all-extras --with dev,powermon` for all optional dependencies

### Quick check (recommended)

Run all CI checks locally with a single command:

```bash
make ci
```

This runs the same checks as CI (pylint for library code, ruff for tests, mypy, pytest with coverage).

### Unified lint/type check via Trunk

Run lint and type checks (including Poetry-managed `pylint` + `mypy`) with one command:

```bash
TRUNK_INTERACTIVE=0 .trunk/trunk check --fix --show-existing
```

This does not run `pytest`; use `make ci` (or `poetry run pytest ...`) for test execution.

### Manual checks

Alternatively, run each check individually:

```bash
poetry run pytest --cov=meshtastic --cov-report=xml
poetry run pylint meshtastic examples/ --ignore-patterns ".*_pb2\.pyi?$"
ruff check meshtastic/tests
poetry run mypy meshtastic/
```

To run the `meshtasticd` simulator integration lane locally (same flow as CI):

```bash
./bin/run-smokevirt-with-meshtasticd.sh
```

This requires Docker and runs stable daemon-focused integration tests in
`meshtastic/tests/test_meshtasticd_ci.py` and
`meshtastic/tests/test_meshtasticd_tcp_interface_ci.py` against a simulated
localhost daemon.

To run the dual-daemon integration lane locally (Linux only; channel
blueprint/export reuse and admin checks across two simulator instances):

```bash
./bin/run-multinode-with-meshtasticd.sh
```

This uses Linux host networking, starts two `meshtasticd` simulators on
`localhost:4401` and `localhost:4402`, and runs
`meshtastic/tests/test_meshtasticd_multinode_ci.py`. Linux is required because
the script depends on host-networking behavior that is not portable to
macOS/Windows.

To run the full legacy smokevirt suite manually:

```bash
MESHTASTICD_PYTEST_TARGETS="meshtastic/tests/test_smokevirt.py" \
MESHTASTICD_PYTEST_MARK_EXPR="smokevirt and not smoke1_destructive" \
./bin/run-smokevirt-with-meshtasticd.sh
```

For hardware-backed serial smoke tests (`meshtastic/tests/test_smoke1.py`):

```bash
make smoke1
```

This runs only the stable non-destructive smoke1 lane (`smoke1 and not
smoke1_destructive`).

To run the destructive lane (reboot/factory reset/config mutation checks):

> Warning: `make smoke1-destructive` reboots the attached device, mutates
> configuration, and can factory-reset the node. Run this only on disposable
> test hardware, or export/backup the device configuration first.

```bash
make smoke1-destructive
```

See [smoke1 firmware compatibility matrix](SMOKE1_FIRMWARE_COMPATIBILITY_MATRIX.md)
for current firmware/CLI expectations and known behavior differences.

For stricter type checking (optional, not required by CI):

```bash
poetry run mypy meshtastic/ --strict
```

### Using GitHub CI actions locally

- You need to have act installed. You can get it at https://nektosact.com/
- `bin/run-ci-local.sh` is the canonical local runner and reads `LOCAL_PYTHON_VERSION` (default defined in that script).
- on Linux: `./bin/run-ci-local.sh`
- on Windows (Git Bash/WSL, POSIX shell syntax):
  - Linux checks (Linux Docker): `LOCAL_PYTHON_VERSION=3.13 ./bin/run-ci-local.sh`
  - Windows checks (Windows host): `act -P windows-latest=-self-hosted --matrix "python-version:${LOCAL_PYTHON_VERSION:-3.13}"`
- on Windows (PowerShell):
  - Linux checks (Linux Docker): `$env:LOCAL_PYTHON_VERSION = "3.13"; ./bin/run-ci-local.sh`
  - Windows checks (Windows host): `$env:LOCAL_PYTHON_VERSION = "3.13"; act -P windows-latest=-self-hosted --matrix "python-version:$env:LOCAL_PYTHON_VERSION"`

The `-P ...=-self-hosted` mapping is optional. It tells `act` to run that job on
your host machine instead of in a container, which can be useful for
performance and for checks that need host-specific behavior. If you prefer
containerized runs, omit the `-P` mapping (as in the Linux-on-Windows example
above).

For more commands see [CI workflow](.github/workflows/ci.yml)
