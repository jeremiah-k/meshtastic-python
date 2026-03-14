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
| P1 | BLE boundary API promotion and adoption | Completed | Public boundary APIs adopted with master-surface compatibility and underscore-compatible test-double fallbacks. |
| P2 | BLEInterface decomposition (lifecycle/receive/management adapters) | In progress | First large extraction landed with service-module delegation and master-surface compatibility parity. |
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
- Added compatibility wrappers in `BLEInterface`, `ConnectionOrchestrator`, `ClientManager`, and `ReconnectScheduler` so production code uses public APIs while underscore-based master/test-double call shapes remain valid.
- Continued P2 boundary-hardening cleanup:
  - fixed disconnect-notification queue flush to avoid full-timeout stalls when queue enqueue fails,
  - switched BLE thread coordinator wrappers to public-first (`create_thread`/`start_thread`) with underscore-compatible fallback,
  - removed lock-held `bluetoothctl trust` subprocess execution in implicit-address management flow,
  - tightened receive-thread duplicate-start guard for "pending start" threads (`ident is None`),
  - aligned `_state_manager_is_connected` fallback to preserve misconfigured-double failure signaling,
  - fixed BLE naming-policy guidance typo in `BLE.md`,
  - restored `_discover`-only discovery-client compatibility for injected runtime/test factories,
  - added `ConnectionOrchestrator` dispatch helper to reduce adapter drift while preserving mock-compatible fallback behavior,
  - decomposed receive-loop orchestration into phase helpers and bounded recovery backoff exponent growth with remaining-cooldown waits.
  - fixed `ConnectionOrchestrator` non-call dispatch to treat unconfigured mock child attributes as missing values,
  - fixed lifecycle start failure handling to clear stale `iface._receiveThread` references before re-raising,
  - moved `connected_elsewhere()` checks out of `_connect_lock` + `_management_lock` critical sections in management command execution,
  - hardened discovery dispatch so unconfigured mock `discover()` no longer masks configured `_discover()` compatibility entrypoints.
- Continued P2 decomposition (line-reduction focused):
  - extracted verified-connect finalization helpers from `ble/interface.py` into `ble/lifecycle_service.py`:
    - `_emit_verified_connection_side_effects`
    - `_discard_invalidated_connected_client`
    - `_finalize_connection_gates`
    - `_is_owned_connected_client`
  - kept `BLEInterface` method names as delegating wrappers for compatibility while moving orchestration body out of `interface.py`.
  - additional correctness fixes integrated during decomposition:
    - receive recovery backoff cap now reaches non-power-of-two configured max values,
    - `BLECompatibilityEventService` queueWork paths now guard against unconfigured mocks,
    - thread-coordinator helper dispatch in `ClientManager` now rejects unconfigured public and underscore mock callables symmetrically,
    - wrapped (`Mock(wraps=...)`) spies are no longer misclassified as unconfigured mock members.
- Continued P2 lifecycle extraction (major `interface.py` reduction):
  - moved disconnect orchestration body from `BLEInterface._handle_disconnect(...)` to `BLELifecycleService._handle_disconnect(...)` and kept `BLEInterface` method as a thin delegating wrapper.
  - moved shutdown orchestration body from `BLEInterface.close()` to `BLELifecycleService._close(...)` and kept `BLEInterface.close()` as a thin delegating wrapper.
  - preserved lock-order and shutdown semantics by passing management wait policy values from `interface.py` into lifecycle service (`_MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS`, `_MANAGEMENT_CONNECT_WAIT_POLL_SECONDS`).
  - this pass reduced `ble/interface.py` from **3121** lines to **2759** lines.
- Continued P2 decomposition pass (interface/connect/client focus):
  - extracted the remaining connect-result verification/publication orchestration (`_verify_and_publish_connected`) into `BLELifecycleService` while preserving patch-sensitive wrapper semantics on `BLEInterface` for status/ownership probes.
  - split `ConnectionOrchestrator._establish_connection(...)` into explicit internal phases:
    - `_prepare_connection_target`,
    - `_resolve_connection_timeouts`,
    - `_attempt_direct_connect`,
    - `_resolve_retry_target`,
    - `_connect_retry_target`.
  - preserved connection failure cleanup semantics during split by ensuring direct/retry clients are closed on all error exits (including direct/retry `BleakDBusError`, keyboard interrupts, and retry fallback failures).
  - separated BLE client setup vs transport responsibilities in `BLEClient`:
    - setup/runtime initialization: `_initialize_runtime_state`, `_initialize_transport_client`,
    - transport execution helpers: `_require_bleak_client`, `_run_transport_operation`,
    - transport wrappers (`connect`/`disconnect`/`read_gatt_char`/`write_gatt_char`/`start_notify`/`stopNotify`) now delegate through shared transport helper.
  - this pass reduced `ble/interface.py` from **2759** lines to **2666** lines.
- Targeted verification completed:
  - `ruff check meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/connection.py meshtastic/interfaces/ble/reconnection.py`
  - `poetry run mypy meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/connection.py meshtastic/interfaces/ble/reconnection.py --strict`
  - `poetry run pytest tests/test_ble_interface_core.py tests/test_ble_connection_edge_cases.py -q` (`223 passed`)
  - `poetry run pytest tests/test_ble_*.py -q` (`411 passed, 3 skipped`)
  - `poetry run ruff check meshtastic/interfaces/ble/{compatibility_service.py,connection.py,reconnection.py,interface.py,lifecycle_service.py,management_service.py} tests/test_ble_interface_core.py BLE.md REFACTOR_PROGRAM-PROGRESS.md`
  - `poetry run mypy meshtastic/interfaces/ble/{compatibility_service.py,connection.py,reconnection.py,interface.py,lifecycle_service.py,management_service.py} --strict`
  - `poetry run pytest tests/test_ble_connection_edge_cases.py tests/test_ble_interface_core.py tests/test_ble_interface_advanced.py -q` (`233 passed`)
  - `poetry run pytest tests/test_ble_integration_scenarios.py tests/test_ble_runner.py tests/test_ble_coordination.py -q` (`38 passed`)
  - `poetry run ruff check meshtastic/interfaces/ble/{discovery.py,errors.py,management_service.py,receive_service.py,connection.py} tests/test_ble_interface_core.py`
  - `poetry run mypy meshtastic/interfaces/ble/{discovery.py,errors.py,management_service.py,receive_service.py,connection.py} --strict`
  - `poetry run pytest tests/test_ble_connection_edge_cases.py tests/test_ble_interface_core.py tests/test_ble_interface_advanced.py tests/test_ble_integration_scenarios.py tests/test_ble_coordination.py tests/test_ble_runner.py -q` (`272 passed`)
  - `source venv/bin/activate && mypy --strict meshtastic/interfaces/ble/{connection.py,discovery.py,lifecycle_service.py,management_service.py,receive_service.py}`
  - `source venv/bin/activate && pytest -q tests/test_ble_connection_edge_cases.py`
  - `source venv/bin/activate && pytest -q tests/test_ble_interface_core.py -k "management_rejects_temp_client_when_target_owned_elsewhere or discovery_manager_accepts_discover_underscore_only_factory or discovery_manager_prefers_configured_underscore_discover_over_unconfigured_mock_public_discover or start_receive_thread_skips_when_interface_closed or start_receive_thread_clears_cached_thread_when_start_fails or discovery_manager_rejects_non_callable_discover_method"`
  - `poetry run pytest -q tests/test_ble_connection_edge_cases.py` (`52 passed`)
  - `poetry run pytest -q tests/test_ble_interface_core.py -k "discard_invalidated_connected_client or finalize_connection_gates or is_owned_connected_client or emit_verified_connection_side_effects or find_device_direct_connect_without_discovery_manager or wait_for_disconnect_notifications_skips_unconfigured_queuework or publish_connection_status_runs_directly_when_queuework_unconfigured or receive_recovery_backoff_reaches_configured_cap_for_non_power_of_two"` (`12 passed`)
  - `poetry run pytest -q tests/test_ble_integration_scenarios.py -k "client_manager_handles_concurrent_updates"` (`1 passed`)
  - `make ci-strict` (`1366 passed, 3 skipped, 92 deselected`; `mypy --strict` clean)
  - `poetry run ruff check meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/lifecycle_service.py`
  - `poetry run mypy meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/lifecycle_service.py --strict`
  - `poetry run pytest -q tests/test_ble_interface_core.py tests/test_ble_interface_advanced.py tests/test_ble_integration_scenarios.py tests/test_ble_connection_edge_cases.py` (`253 passed`)
  - `make ci-strict` (`1366 passed, 3 skipped, 92 deselected`; `mypy --strict` clean)
  - `poetry run ruff check meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/lifecycle_service.py meshtastic/interfaces/ble/connection.py meshtastic/interfaces/ble/client.py`
  - `poetry run mypy meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/lifecycle_service.py meshtastic/interfaces/ble/connection.py meshtastic/interfaces/ble/client.py --strict`
  - `poetry run pytest -q tests/test_ble_interface_core.py -k "finalize_connection_gates_cleans_up_when_client_loses_ownership_mid_finalize or is_owned_connected_client_reads_status_tuple or connect_raises_when_registry_ownership_is_lost_after_gate_finalization or connect_restores_requested_identifier_after_name_target_loses_ownership or connect_rechecks_ownership_before_publishing_connected"` (`6 passed`)
  - `poetry run pytest -q tests/test_ble_connection_edge_cases.py -k "connection_orchestrator_interrupt_resets_state_and_closes_client or connection_orchestrator_reraises_retry_ble_dbus_error or connection_orchestrator_handles_bleak_dbus_error_during_connect"` (`3 passed`)
  - `poetry run pytest -q tests/test_ble_interface_core.py tests/test_ble_interface_advanced.py tests/test_ble_connection_edge_cases.py tests/test_ble_client_edge_cases.py tests/test_ble_integration_scenarios.py` (`299 passed`)
  - `make ci-strict` (`1366 passed, 3 skipped, 92 deselected`; `mypy --strict` clean)

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

- Continue reducing connect-path orchestration density (`_establish_and_update_client`, connect gate claim sequencing, and final state publication flow) into dedicated collaborators.
- Finish reducing `BLEInterface` direct orchestration density while keeping lock-order semantics and compatibility shims unchanged.

### Decomposition Hotspots (Current)

Current `wc -l` (2026-03-14):

- `meshtastic/interfaces/ble/interface.py`: **2666**
- `meshtastic/interfaces/ble/connection.py`: **1274**
- `meshtastic/interfaces/ble/client.py`: **1002**
- `meshtastic/interfaces/ble/gating.py`: **768**
- `meshtastic/interfaces/ble/runner.py`: **729**
- `meshtastic/interfaces/ble/discovery.py`: **668**
- `meshtastic/interfaces/ble/reconnection.py`: **603**

`interface.py` trend:

- `40835ba`: 3674 lines
- `59fc5e5`: 3203 lines
- `1e89a17` (previous decomposition commit): 3121 lines
- `a6aa65f` (disconnect/close extraction): 2759 lines
- current working tree: **2666** lines

Next active reduction targets:

1. `ble/interface.py`: continue reducing connect path density by extracting `_establish_and_update_client` and related state mutation sequencing into lifecycle/connection collaborators.
2. `ble/connection.py`: move new connection phase helpers into a dedicated internal module/service to reduce file size while retaining current behavior and tests.
3. `ble/client.py`: move transport helper layer into a dedicated internal module/service and keep `BLEClient` as thin compatibility/public wrapper.
