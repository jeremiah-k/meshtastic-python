# mtjk

`mtjk` is a separately published distribution of the Meshtastic Python
library, maintained while changes are validated and selectively upstreamed.

Work began in September 2025 to stabilize connection interfaces and message
delivery for [mmrelay](https://github.com/jeremiah-k/meshtastic-matrix-relay).
The modernization work keeps existing Meshtastic Python usage compatible:

- package import namespace remains `meshtastic`
- CLI command remains `meshtastic`
- existing API compatibility is intentionally preserved

It is intended to be a drop-in, backward-compatible replacement for upstream.

## Project Status

This is a **temporary fork** of
[meshtastic/python](https://github.com/meshtastic/python). It exists to ship
fixes and improvements while they are validated and upstreamed selectively.

**This repository does not accept pull requests.** Community development efforts should be directed to the [upstream project](https://github.com/meshtastic/python). If you find a fix or improvement here that you would like to carry upstream, please do so.

## Notable Changes

- major BLE and interface internals were refactored for maintainability while keeping compatibility shims in place
- concurrency and lifecycle paths were tightened to reduce race-condition and shutdown edge cases
- CI and release workflows were modernized, including Trusted Publisher-based PyPI release flow

For technical details, see:
- [REFACTOR_PROGRAM.md](REFACTOR_PROGRAM.md): rationale and early change log for the major refactor work maintained here.
- [COMPATIBILITY.md](COMPATIBILITY.md): canonical inventory of compatibility shims, deprecations, and migration mapping.
- [CONTRIBUTING.md](CONTRIBUTING.md): local setup, CI-equivalent checks, and contributor workflow conventions.

## Install the CLI (recommended: pipx)

`pipx` is recommended for CLI tools so each app gets an isolated environment.

### 1) Remove prior installs first (recommended)

If you previously installed upstream `meshtastic`, remove it before installing `mtjk`.

```bash
# If installed with pipx:
pipx uninstall meshtastic || true

# If installed with pip in a Python environment:
python3 -m pip uninstall -y meshtastic
```

### 2) Install `mtjk`

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
- Use `mtjk` in dependency specs for this package.
- The package does not provide `import mtjk`.

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

Report `mtjk`-specific issues here:
- <https://github.com/jeremiah-k/meshtastic-python/issues>

Please do not file `mtjk`-specific issues with upstream maintainers.

## Release Notes (Maintainers)

- Versions match upstream releases with a `.postN` suffix (e.g., `2.7.8.post1` is the first `mtjk` release based on upstream `2.7.8`).
- Create a GitHub release with tag `vX.Y.Z[.postN]` (or push the tag manually). This triggers the PyPI publish workflow via Trusted Publisher.
- Trusted Publisher workflow expects the git tag version to match `pyproject.toml` exactly.
- Supported tag formats are `vX.Y.Z...` or `X.Y.Z...` (both map to the same package version check).
- PyPI Trusted Publisher must match this repo/workflow/environment tuple:
  `jeremiah-k/meshtastic-python` + `.github/workflows/pypi-publish.yml` + `pypi-release`.
