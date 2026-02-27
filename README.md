# Meshtastic Python

<div align="center" markdown="1">

<img src=".github/meshtastic_logo.png" alt="Meshtastic Logo" width="80"/>

  <h1 align="center"> Meshtastic Python
</h1>
  <p style="font-size:15px;" align="center">A Python library and client for use with Meshtastic devices. </p>

[![codecov](https://codecov.io/gh/meshtastic/python/branch/master/graph/badge.svg?token=TIWPJL73KV)](https://codecov.io/gh/meshtastic/python)
![PyPI - Downloads](https://img.shields.io/pypi/dm/meshtastic)
[![CI](https://img.shields.io/github/actions/workflow/status/meshtastic/python/ci.yml?branch=master&label=actions&logo=github&color=yellow)](https://github.com/meshtastic/python/actions/workflows/ci.yml)
[![CLA assistant](https://cla-assistant.io/readme/badge/meshtastic/python)](https://cla-assistant.io/meshtastic/python)
[![Fiscal Contributors](https://opencollective.com/meshtastic/tiers/badge.svg?label=Fiscal%20Contributors&color=deeppink)](https://opencollective.com/meshtastic/)
![GPL-3.0](https://img.shields.io/badge/License-GPL%20v3-blue.svg)

</div>

<div align="center">
	<a href="https://meshtastic.org/docs/software/python/cli/installation">Getting Started Guide</a>
	-
	<a href="https://python.meshtastic.org">API Documentation</a>
</div>

## Overview

This small library (and example application) provides an easy API for sending and receiving messages over mesh radios.
It also provides access to any of the operations/data available in the device user interface or the Android application.
Events are delivered using a publish-subscribe model, and you can subscribe to only the message types you are interested in.

## Local Development Setup

1. Clone the repository and initialize submodules:
   ```bash
   git submodule update --init --recursive
   ```
1. Prerequisites:
   - Requires Python 3.10+.
   - Type hints in this repo use PEP 604 unions and built-in generics (`X | Y`, `list[...]`, `dict[...]`).
1. Install dependencies with Poetry:
   ```bash
   poetry install --all-extras --with dev,powermon
   ```
1. Run the same checks as CI:
   ```bash
   make ci
   ```

For more contributor workflow details, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Call for Contributors

This library and CLI has gone without a consistent maintainer for a while, and there's many improvements that could be made. We're all volunteers here and help is extremely appreciated, whether in implementing your own needs or helping maintain the library and CLI in general.

If you're interested in contributing but don't have specific things you'd like to work on, look at the roadmap below!

## Roadmap

This should always be considered a list in progress and flux -- inclusion doesn't guarantee implementation, and exclusion doesn't mean something's not wanted. GitHub issues are a great place to discuss ideas.

- ✅ Types
  - Codebase is `mypy --strict` compatible.
  - CI still runs `mypy meshtastic/` (non-strict); strict is available via `make ci-strict`.
- 🟡 Async-friendliness
  - BLE lifecycle/reconnect internals were substantially refactored and race-hardened.
  - Additional async-friendly API improvements are still open.
- 🟡 API consistency and compatibility
  - Broad camelCase normalization with explicit compatibility shims is in place across key modules.
  - Historical BLE compatibility surface from 2.7.7 is preserved.
- ✅ Example readability
  - Examples were updated and simplified to emphasize library usage patterns
- ⏳ CLI completeness and consistency
  - Support full firmware feature coverage in the CLI.
  - Provide stable, script-friendly output formats.
- ⏳ CLI input validation and documentation
  - Clarify compatible/incompatible argument combinations.
  - Document pubsub events and common workflows.
- ⏳ Helpers for third-party code
  - Provide reusable helpers so external tools can share CLI-style connection options.
- ⏳ Data storage and processing
  - Standardize packet recording for post-analysis/debugging.
  - Evaluate persistence beyond nodedb (for example sqlite-backed tooling).
  - Expand maps/charts/visualization support.

## Stats

![Alt](https://repobeats.axiom.co/api/embed/c71ee8fc4a79690402e5d2807a41eec5e96d9039.svg "Repobeats analytics image")
