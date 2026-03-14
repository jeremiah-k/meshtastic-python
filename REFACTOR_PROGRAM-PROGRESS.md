# Refactor Program Progress (Disposable)

## Purpose

This is an internal working tracker for large-pass execution.

- It is expected to change frequently.
- It is expected to be deleted once refactor program goals are complete.
- Canonical high-level rationale stays in `REFACTOR_PROGRAM.md` and `REFACTOR_PROGRAM-2.md`.

## Working Rules

1. Execute large, subsystem-focused passes.
2. Prefer behavior-preserving extraction/boundary cleanup first.
3. Update tests in the same pass.
4. Run targeted lint/type/tests for touched areas after each pass.
5. Do not split one coherent pass into many micro-prompts.

## Pass Board

| Pass | Theme | Status | Notes |
| --- | --- | --- | --- |
| P1 | BLE boundary API promotion and adoption | Completed | Public boundary APIs adopted with legacy test-double compatibility fallbacks. |
| P2 | BLEInterface decomposition (lifecycle/receive/management adapters) | In progress | First large extraction landed with service-module delegation and compatibility parity. |
| P3 | MeshInterface inbound dispatch split | Planned | `_handle_from_radio` decomposition with dispatch map. |
| P4 | Node `setURL()` transaction extraction | Planned | Planner/apply/rollback separation. |
| P5 | Compat isolation and hot-path cleanup | Planned | Move shims out of core runtime paths where feasible. |
| P6 | Test architecture and CI lane cleanup | Planned | Split large behavior domains; clarify lane intent. |

## Pass P1 Plan (Current)

### Scope

- Promote collaborator boundary APIs in BLE modules.
- Switch BLE orchestration code to call collaborator public APIs.
- Preserve existing behavior and compatibility.

### Acceptance Criteria

- `ble/interface.py` no longer directly calls collaborator private members in normal orchestration paths.
- Collaborator modules expose public entrypoints for previously required private behaviors.
- Existing tests are updated where strict doubles assumed underscore-only methods.
- Targeted BLE lint/type/tests pass.

### P1 Task Checklist

- [x] Add public state access/transition methods in `ble/state.py`.
- [x] Add public thread/event lifecycle methods in `ble/coordination.py`.
- [x] Add public subscription APIs in `ble/notifications.py`.
- [x] Add public `discover_devices()` in `ble/discovery.py`.
- [x] Add public validator/client/orchestrator APIs in `ble/connection.py`.
- [x] Add public scheduler APIs in `ble/reconnection.py`.
- [x] Add public retry-policy accessors in `ble/policies.py`.
- [x] Add public error-handler wrappers in `ble/errors.py`.
- [x] Switch `ble/interface.py` to public collaborator calls.
- [x] Switch `ble/connection.py` and `ble/reconnection.py` collaborator calls to public APIs.
- [x] Update discovery scan path to prefer public `discover()` with compatibility fallback.
- [x] Update/repair BLE tests for any underscore-only test doubles.
- [x] Run targeted BLE lint/type/tests and fix failures.

## Execution Log

### 2026-03-14

- Started P1.
- Implemented boundary API promotion across BLE collaborators.
- Replaced direct collaborator-private calls in BLE interface/runtime code with public collaborator APIs.
- Completed compatibility fallbacks for underscore-only test doubles used across BLE tests.
- Added compatibility wrappers in `BLEInterface`, `ConnectionOrchestrator`, `ClientManager`, and `ReconnectScheduler` so production code uses public APIs while tests/mocks with underscore methods remain valid.
- Targeted verification completed:
  - `ruff check meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/connection.py meshtastic/interfaces/ble/reconnection.py`
  - `poetry run mypy meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/connection.py meshtastic/interfaces/ble/reconnection.py --strict`
  - `poetry run pytest tests/test_ble_interface_core.py tests/test_ble_connection_edge_cases.py -q` (`223 passed`)
  - `poetry run pytest tests/test_ble_*.py -q` (`411 passed, 3 skipped`)

## Pass P2 Plan (Next)

### Scope

- Split `BLEInterface` responsibilities into clearer internal boundaries:
  - lifecycle/connect-disconnect service,
  - receive/recovery service,
  - management operations service,
  - compatibility/event publication adapter.
- Keep behavior stable and keep `BLEInterface` public entrypoints delegating.

### Initial Tasks

- Identify high-churn orchestration clusters in `ble/interface.py` and define extraction seams.
- Introduce private helper classes/modules with constructor-injected collaborators.
- Move logic in chunks with delegation shims and no behavior changes.
- Keep targeted BLE test slice green after each extraction chunk.

## Pass P2 Progress (Current)

### Completed In This Pass

- Added service modules:
  - `meshtastic/interfaces/ble/lifecycle_service.py`
  - `meshtastic/interfaces/ble/receive_service.py`
  - `meshtastic/interfaces/ble/management_service.py`
  - `meshtastic/interfaces/ble/compatibility_service.py`
- Delegated `BLEInterface` method clusters into those services while preserving public/private entrypoint names on `BLEInterface`.
- Preserved monkeypatch/test compatibility by routing patch-sensitive dependencies (`BLEClient`, `_is_currently_connected_elsewhere`, `publishingThread`, `sys`, `shutil`, `subprocess`) through `interface.py` wrappers.
- Kept behavior parity with targeted strict typing and BLE test slices.

### Verification (This Pass)

- `ruff check meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/lifecycle_service.py meshtastic/interfaces/ble/receive_service.py meshtastic/interfaces/ble/compatibility_service.py meshtastic/interfaces/ble/management_service.py`
- `poetry run mypy meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/lifecycle_service.py meshtastic/interfaces/ble/receive_service.py meshtastic/interfaces/ble/compatibility_service.py meshtastic/interfaces/ble/management_service.py --strict`
- `poetry run pytest tests/test_ble_interface_core.py tests/test_ble_connection_edge_cases.py -q` (`223 passed`)
- `poetry run pytest tests/test_ble_*.py -q` (`411 passed, 3 skipped`)

### Remaining P2 Work

- Move connect/disconnect lifecycle core (`_handle_disconnect`, connect-finalization helpers, close teardown sequencing) into a dedicated lifecycle runtime service.
- Finish reducing `BLEInterface` direct orchestration density while keeping lock-order semantics and compatibility shims unchanged.
