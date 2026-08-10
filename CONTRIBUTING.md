# mtjk Development Guide

> **This repository does not accept pull requests.** `mtjk` is maintained
> separately on a temporary basis. Community contributions should be directed to
> the upstream project at
> [meshtastic/python](https://github.com/meshtastic/python).
>
> This document is retained as **developer documentation** for anyone who needs
> to understand or maintain this repository: codebase structure, local checks,
> and CI flow.

## Development resources

- `mtjk` repository: <https://github.com/jeremiah-k/mtjk>
- `mtjk` issue tracker: <https://github.com/jeremiah-k/mtjk/issues>
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

### Updating protobufs

To update the protobuf submodule and regenerate `meshtastic/protobuf/*_pb2.py`
and `*_pb2.pyi` files:

```bash
make protobufs-update
```

The generator needs a `protoc` compiler. The CI workflow uses the `protoc`
binary bundled with nanopb's Linux release package from
<https://jpa.kapsi.fi/nanopb/download/>:

```bash
curl -fsSL -o nanopb-0.4.9.1-linux-x86.tar.gz \
  https://jpa.kapsi.fi/nanopb/download/nanopb-0.4.9.1-linux-x86.tar.gz
printf '%s  %s\n' \
  951a9ab2385424a4cdf245d0c84f4c88c6ccbc65a0dade4b246d50c068f24128 \
  nanopb-0.4.9.1-linux-x86.tar.gz | sha256sum -c -
tar xzf nanopb-0.4.9.1-linux-x86.tar.gz
mv nanopb-0.4.9.1-linux-x86 nanopb-0.4.9.1
```

The `nanopb-*` directory is intentionally ignored by git. If you already have
another compatible `protoc`, run `PROTOC=/path/to/protoc ./bin/regen-protobufs.sh`.
To allow discovery from `PATH`, run
`ALLOW_SYSTEM_PROTOC=1 ./bin/regen-protobufs.sh`.

### Quick check (recommended)

Run all CI checks locally with a single command:

```bash
make ci
```

This runs the same checks as CI (pylint for library code, ruff for tests, mypy, pytest with coverage).

### Quality-tool ownership

Trunk is the repository-wide linter orchestrator and the version source for
standalone tools such as Ruff, Black, isort, ShellCheck, and the security
scanners. Their pins live in `.trunk/trunk.yaml`; do not copy them into a
second versions file. The standalone Ruff CI job reads its install version
directly from that configuration through `bin/check_quality_tool_versions.py`.

Poetry owns the Python environment and pins project-aware Python tools such as
Pylint and Mypy in `pyproject.toml` and `poetry.lock`. Trunk's
`pylint-poetry` and `mypy-poetry` definitions orchestrate those
Poetry-installed tools rather than installing competing copies. Pylint behavior
is configured in `.pylintrc`, and Ruff behavior is configured in `ruff.toml`
plus Trunk's managed base configuration.

The Poetry application version used to install those dependencies is a build
tool rather than a project dependency. It is pinned in CI and container builds,
and one Renovate custom manager updates every `poetry==X.Y.Z` installation in a
single dependency branch. The container-only export plugin is independently
pinned and maintained by Renovate so image dependency resolution is
reproducible without adding the plugin to every development environment.

Mypy is the canonical Python type checker for this repository. Pyright is not
part of the quality gate: maintaining two overlapping type-checker baselines
added cost without a distinct compatibility guarantee, and a static Pyright
virtualenv path cannot reliably identify Poetry's environment across machines.

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
poetry run pylint meshtastic examples/
.trunk/trunk check --filter=ruff meshtastic/tests tests
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

For more commands see [CI workflow](.github/workflows/ci.yml)
