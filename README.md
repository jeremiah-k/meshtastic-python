# mtjk (Meshtastic Python Fork)

`mtjk` is a fork of the Meshtastic Python project, published as `mtjk`.

It is intended to be a drop-in, backward-compatible replacement for upstream:
- package import namespace remains `meshtastic`
- CLI command remains `meshtastic`
- existing API compatibility is intentionally preserved

`mtjk` is a maintained fork, published separately while changes are validated and selectively upstreamed.
Work on this fork began in September 2025; early BLE-focused details are in [BLE.md](BLE.md).

## What Changed in This Fork

- major BLE and interface internals were refactored for maintainability while keeping compatibility shims in place
- concurrency and lifecycle paths were tightened to reduce race-condition and shutdown edge cases
- CI and release workflows were modernized, including Trusted Publisher-based PyPI release flow

For technical details, see:
- [COMPATIBILITY.md](COMPATIBILITY.md): canonical inventory of compatibility shims, deprecations, and migration mapping.
- [BLE.md](BLE.md): BLE architecture notes, lifecycle behavior, and BLE-specific implementation guidance.
- [REFACTOR_PROGRAM.md](REFACTOR_PROGRAM.md): rationale and change log for the major refactor work in this fork.
- [CONTRIBUTING.md](CONTRIBUTING.md): local setup, CI-equivalent checks, and contributor workflow conventions.

## Install the CLI (recommended: pipx)

`pipx` is recommended for CLI tools so each app gets an isolated environment.

### 1) Remove prior installs first (recommended)

If you previously installed upstream `meshtastic`, remove it before installing this fork.

```bash
# If installed with pipx:
pipx uninstall meshtastic || true

# If installed with pip in a Python environment:
python3 -m pip uninstall -y meshtastic
```

### 2) Install this fork

```bash
pipx install mtjk
```

### 3) Verify

```bash
meshtastic --version
```

### Install latest from Git (`develop`)

To install the latest unreleased version from this repository (clean install):

```bash
# If you previously installed upstream via pipx, remove it first:
pipx uninstall meshtastic || true

pipx uninstall mtjk || true
pipx install "git+https://github.com/jeremiah-k/meshtastic-python.git@develop"
```

## Upgrade / Uninstall

```bash
pipx upgrade mtjk
pipx uninstall mtjk
```

## Developer Usage (existing Meshtastic API)

Dependency name is `mtjk`, but import namespace remains `meshtastic`.

Important:
- If your dependency spec says `meshtastic`, you will install upstream.
- Use `mtjk` in dependency specs to install this fork.
- This fork does not provide `import mtjk`.

### requirements.txt

```text
mtjk
```

Unreleased from Git:

```text
mtjk @ git+https://github.com/jeremiah-k/meshtastic-python.git@develop
```

If you need optional CLI extras in a dependency spec:

```text
mtjk[cli]
mtjk[cli] @ git+https://github.com/jeremiah-k/meshtastic-python.git@develop
```

### pyproject.toml (PEP 621)

```toml
[project]
dependencies = [
  "mtjk",
]
```

Unreleased from Git:

```toml
[project]
dependencies = [
  "mtjk @ git+https://github.com/jeremiah-k/meshtastic-python.git@develop",
]
```

### setup.cfg

```ini
[options]
install_requires =
    mtjk
```

Unreleased from Git:

```ini
[options]
install_requires =
    mtjk @ git+https://github.com/jeremiah-k/meshtastic-python.git@develop
```

### Python import (unchanged)

```python
import meshtastic
import meshtastic.serial_interface

interface = meshtastic.serial_interface.SerialInterface()
interface.sendText("hello mesh")
interface.close()
```

## Support

Report issues for this fork here:
- <https://github.com/jeremiah-k/meshtastic-python/issues>

Please do not file fork-specific issues with upstream maintainers.

## Release Notes (Maintainers)

- Trusted Publisher workflow expects the git tag version to match `pyproject.toml` exactly.
- Supported tag formats are `vX.Y.Z...` or `X.Y.Z...` (both map to the same package version check).
- PyPI Trusted Publisher must match this repo/workflow/environment tuple:
  `jeremiah-k/meshtastic-python` + `.github/workflows/pypi-publish.yml` + `pypi-release`.
