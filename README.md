# mtjk (Meshtastic Python Fork)

`mtjk` is a work-in-progress fork of the Meshtastic Python project.

It is intended to be a drop-in, backward-compatible replacement for upstream:
- package import namespace remains `meshtastic`
- CLI command remains `meshtastic`
- existing API compatibility is intentionally preserved

This fork is temporary. The goal is to upstream what proves valuable after hardening and validation.

## Install the CLI (recommended: pipx)

`pipx` is recommended for CLI tools so each app gets an isolated environment.

### 1) Remove prior installs first (recommended)

If you previously installed upstream `meshtastic`, remove it before installing this fork.

```bash
# If installed with pipx:
pipx uninstall meshtastic || true
pipx uninstall mtjk || true

# If installed with pip in a Python environment:
python3 -m pip uninstall -y meshtastic mtjk
```

### 2) Install this fork

```bash
pipx install mtjk
```

### 3) Verify

```bash
meshtastic --version
```

## Upgrade / Uninstall

```bash
pipx upgrade mtjk
pipx uninstall mtjk
```

## Developer Usage (existing Meshtastic API)

```python
import meshtastic
import meshtastic.serial_interface

interface = meshtastic.serial_interface.SerialInterface()
interface.sendText("hello mesh")
interface.close()
```

## What Changed in This Fork

- major BLE and interface internals were refactored for maintainability while keeping compatibility shims in place
- concurrency and lifecycle paths were tightened to reduce race-condition and shutdown edge cases
- CI and release workflows were modernized, including Trusted Publisher-based PyPI release flow

For technical details, see:
- `COMPATIBILITY.md`
- `REFACTOR_PROGRAM.md`
- `CONTRIBUTING.md`

## Support

Report issues for this fork here:
- <https://github.com/jeremiah-k/meshtastic-python/issues>

Please do not file fork-specific issues with upstream maintainers.
