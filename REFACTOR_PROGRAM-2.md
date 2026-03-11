# Meshtastic Python Refactor Program - Phase 2

Status: Active continuation plan (March 11, 2026)

This file is intentionally in the repository root (next to `REFACTOR_PROGRAM.md`)
for side-by-side reviewer access.

## 1) Purpose

`REFACTOR_PROGRAM.md` captures the rationale and outcomes of the large
cross-cutting refactor pass. This document defines the continuation plan needed
to finish stabilization safely.

The key change in approach is explicit: future work is split by subsystem, not
as another repository-wide "refactor" pass.

## 2) Inputs and Constraints

This plan is built from two detailed review syntheses and the current repository
state:

- Branch size and risk profile are atypical for maintenance:
  - ~150 files changed
  - ~60k insertions / ~10k deletions
  - broad impact across BLE, mesh, CLI, node, transport, tests, and workflows
- Major implementation quality gains are real, but integration risk remains high
  because multiple critical surfaces changed simultaneously.
- Release workflow modernization is required:
  - adopt Trusted Publisher (OIDC) for PyPI
  - simplify publication flows
  - separate PyPI publishing from standalone asset handling

## 3) Program Principles

1. One subsystem per branch and PR.
2. One behavior-contract change family per branch.
3. CI/docs workflow changes in dedicated branch unless required by a subsystem.
4. No opportunistic "while here" changes outside scope fences.
5. Regressions are attributed by subsystem branch, not by giant mixed batches.

## 4) Workstream Decomposition

The next round is split into five tracked subsystems.

### 4.1 BLE internals only

Scope:

- `meshtastic/interfaces/ble/*`
- BLE-specific tests under `tests/test_ble_*` and focused compatibility tests

Out of scope:

- `mesh_interface.py` wait-state semantics
- CLI behavior changes
- non-BLE transport (TCP/stream/OTA) changes

Exit criteria:

- no behavior drift in documented BLE compatibility matrix
- targeted BLE stress/reconnect suites stable
- no cross-subsystem file churn

### 4.2 Mesh wait-state/callback model only

Scope:

- `meshtastic/mesh_interface.py` request-scoped wait/error/ack model
- related tests in `meshtastic/tests/test_mesh_interface.py`

Out of scope:

- BLE connection orchestration internals
- CLI parsing or command behavior
- transport reconnect policy

Exit criteria:

- scoped/unscoped wait lanes have explicit invariants and tests
- callback timing paths (summary emit, ack, error) are deterministic under tests
- no lock-order regressions in touched paths

### 4.3 CLI/config only

Scope:

- `meshtastic/__main__.py`
- config export/configure parity logic
- CLI-focused tests in `meshtastic/tests/test_main.py` and related config tests

Out of scope:

- BLE core
- transport reconnect implementation
- mesh core wait/callback internals

Exit criteria:

- explicit local vs broadcast ACK behavior tested and documented
- export->configure->export parity remains stable
- secret redaction behavior is precise and non-overbroad

### 4.4 Transport/reconnect only

Scope:

- `meshtastic/tcp_interface.py`
- `meshtastic/stream_interface.py`
- `meshtastic/ota.py`
- shared host parsing in `meshtastic/host_port.py`

Out of scope:

- CLI command semantics
- BLE lifecycle orchestration
- mesh callback/wait model

Exit criteria:

- reconnect/error handling preserves deterministic semantics
- fd-state vs non-fd-state error classification remains correct
- host parser behavior covered for explicit-port and host-only paths

### 4.5 Docs/CI only

Scope:

- policy docs and roadmap updates
- CI/workflow modernization
- release pipeline simplification and Trusted Publisher setup

Out of scope:

- runtime behavior changes

Exit criteria:

- release automation split into minimal workflows with clear responsibilities
- OIDC Trusted Publisher flow tested in fork
- roadmap reflects actual staged plan and status

## 5) Branch and PR Operating Model

Recommended branch naming:

- `ble-int-*`
- `mesh-wait-*`
- `cli-config-*`
- `transport-*`
- `docs-ci-*`

PR requirements:

- include explicit "in scope" and "out of scope" block
- include subsystem-specific test command list
- include risk notes for behavior-contract changes
- reject mixed-subsystem changes unless required for unblocking and documented

## 6) Validation Gates by Subsystem

Common baseline for all workstreams:

- `poetry run ruff check <touched files>`
- `poetry run pytest <touched tests>`

Additional gates:

- BLE internals:
  - BLE core/advanced/gating/runner test suites
  - reconnect stress coverage
- Mesh wait-state:
  - full `test_mesh_interface.py` pass
  - scoped wait race coverage
- CLI/config:
  - full `test_main.py`
  - config/export/configure tests
- Transport/reconnect:
  - TCP/stream/OTA targeted suites
  - host parser tests
- Docs/CI:
  - workflow lint/sanity checks
  - dry-run fork validation notes

## 7) Risk Register and Mitigations

### 7.1 High risk: oversized core files

Risk:

- `meshtastic/interfaces/ble/interface.py`
- `meshtastic/mesh_interface.py`
- `meshtastic/__main__.py`
- `meshtastic/node.py`

Mitigation:

- no broad rewrites now
- isolate behavior changes by subsystem
- perform decomposition only after stabilization gates pass

### 7.2 High risk: compatibility drift while "cleaning up"

Risk:

- removing/altering historical shims in mixed refactors

Mitigation:

- keep `COMPATIBILITY.md` + marker checks as contract
- require explicit compatibility note in PR description

### 7.3 High risk: cross-subsystem regressions hidden by large batches

Mitigation:

- strict scope fences and branch separation
- subsystem-specific test matrices
- avoid "drive-by" edits

### 7.4 Medium risk: release pipeline complexity and manual coupling

Mitigation:

- split release concerns:
  - PyPI trusted publish workflow
  - standalone release-assets workflow
- remove automated version bump/push from publish path

## 8) Release Workflow Modernization Plan

### 8.1 Objective

Simplify publishing and align with Trusted Publisher (OIDC) used in other
maintainer projects.

### 8.2 Implemented target shape

Two workflows:

1. `pypi-publish.yml`

- trigger: release published and optional manual dispatch
- build sdist/wheel
- run `twine check`
- publish via `pypa/gh-action-pypi-publish` with `id-token: write`

1. `release-assets.yml`

- separate standalone/binary asset build and GitHub release upload
- decoupled from PyPI publish success/failure

### 8.3 Why split

- PyPI publication should be minimal, deterministic, and auditable.
- Asset packaging has different dependencies and failure modes.
- Decoupling improves triage and prevents unnecessary release coupling.

### 8.4 Trusted Publisher checklist

For fork validation and upstream adoption:

1. Configure PyPI Trusted Publisher:
   - owner/repo
   - workflow filename
   - environment name
2. Confirm environment protection rules in GitHub.
3. Trigger release in fork and verify OIDC publish path.
4. Mirror final tuple exactly in upstream configuration.

## 9) Documentation Plan

`REFACTOR_PROGRAM.md` remains the historical architecture/rationale record.

This `REFACTOR_PROGRAM-2.md` file is the active execution plan and tracking
anchor for staged completion work.

When a stage completes:

- update this file with completion notes and residual risks
- link to merged PR(s) by subsystem
- keep status entries concise and factual

## 10) Stage Sequence

### Stage A (current): docs/CI foundation

- establish subsystem-split plan (this document)
- split release workflows
- set Trusted Publisher validation path

### Stage B: BLE internals stabilization

- resolve remaining BLE ownership/gating/reconnect edge issues in isolation

### Stage C: mesh wait-state/callback stabilization

- finish scoped wait/callback correctness work

### Stage D: CLI/config stabilization

- finish CLI behavior parity and config/export precision fixes

### Stage E: transport/reconnect stabilization

- finish TCP/stream/OTA/host parser hardening and regression closure

## 11) Definition of Done for Phase 2

Phase 2 is considered complete when:

- each subsystem track has shipped independently with scoped PRs
- release publishing is OIDC trusted-publisher based
- remaining high-risk behavior deltas are documented and tested
- no pending mixed-subsystem "catch-all refactor" PR remains
