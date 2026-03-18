# BLE Pass 1 Progress (Temporary)

> Temporary architect-facing progress log for AGOR PASS 1.
> Last updated: 2026-03-17
> Status: **PASS 1 complete (BLE runtime ownership transfer criteria satisfied in this fork snapshot)**

## Scope (Current Tranche)

- Retire remaining runtime static-core authority in BLE receive/recovery and management paths.
- Keep BLEInterface as facade and preserve compatibility surfaces.
- Preserve behavior while shifting implementation ownership to instance-bound collaborators.

## Interface-authority calls removed

- Removed static receive runtime reliance on direct interface authority calls in `receive_service.py`:
  - Removed static-path `iface.close()` control authority in payload-read fatal branches.
  - Removed static-path `iface._recover_receive_thread(...)` control authority in receive-loop fatal branches.
- Replaced those paths with controller-owned runtime behavior via `BLEReceiveRecoveryController`.

## Runtime authority retired

- `BLEReceiveRecoveryService._read_and_handle_payload`
  - Runtime owner now: `BLEReceiveRecoveryController._read_and_handle_payload()`.
  - Compatibility shim remains: static service method delegates.
- `BLEReceiveRecoveryService._handle_payload_read`
  - Runtime owner now: `BLEReceiveRecoveryController._handle_payload_read()`.
  - Compatibility shim remains: static service method delegates.
- `BLEReceiveRecoveryService._run_receive_cycle`
  - Runtime owner now: `BLEReceiveRecoveryController._run_receive_cycle()`.
  - Compatibility shim remains: static service method delegates.
- `BLEReceiveRecoveryService._receive_from_radio_impl`
  - Runtime owner now: `BLEReceiveRecoveryController.receive_from_radio_impl()`.
  - Compatibility shim remains: static service method delegates.
- `BLEReceiveRecoveryService._read_from_radio_with_retries`
  - Runtime owner now: `BLEReceiveRecoveryController.read_from_radio_with_retries()`.
  - Compatibility shim remains: static service method delegates.
- `BLEReceiveRecoveryService._handle_transient_read_error`
  - Runtime owner now: `BLEReceiveRecoveryController.handle_transient_read_error()`.
  - Compatibility shim remains: static service method delegates.
- `BLEReceiveRecoveryService._log_empty_read_warning`
  - Runtime owner now: `BLEReceiveRecoveryController.log_empty_read_warning()`.
  - Compatibility shim remains: static service method delegates.
- `BLEManagementCommandsService` runtime wrappers:
  - `_start_management_phase`, `_resolve_management_target`, `_acquire_client_for_target`, `_execute_with_client`, `_execute_management_command`, `_validate_*`, `pair`, `unpair`, `_run_bluetoothctl_trust_command`, and `trust` now delegate to `BLEManagementCommandHandler`.
- `BLEManagementCommandHandler` now owns runtime implementations for:
  - management timeout validation,
  - connect-timeout override validation,
  - bluetoothctl trust command execution,
  - trust lifecycle flow.

## Remaining runtime authority on static service cores

- Lifecycle still exposes many static service helpers in `lifecycle_service.py`.
  - Why it still exists: compatibility/test shim surface remains broad and heavily exercised.
  - Classification: mostly compatibility shims and helper utilities; lifecycle runtime entrypoints already delegate to coordinator owners.
  - What is required to retire further: split lifecycle compatibility shim surface from helper surface into smaller modules and migrate remaining helper callsites to direct coordinator-owned helpers.

## Runtime-owner map

- shutdown: `BLEShutdownLifecycleCoordinator`
- disconnect: `BLEDisconnectLifecycleCoordinator`
- receive loop: `BLEReceiveRecoveryController`
- receive recovery: `BLEReceiveRecoveryController`
- management command execution: `BLEManagementCommandHandler`
- connection verification/publication: `BLEConnectionOwnershipLifecycleCoordinator`

## Validation evidence (this tranche)

- `ruff check meshtastic/interfaces/ble/receive_service.py meshtastic/interfaces/ble/management_service.py tests/test_ble_lifecycle_receive_targets.py tests/test_ble_utils_service_targets.py` -> passed.
- `pytest -q tests/test_ble_lifecycle_receive_targets.py tests/test_ble_utils_service_targets.py tests/test_ble_interface_core.py -k "receive or management or safe_execute or retry_policy or publishing_thread or malformed_fromnum"` -> **110 passed, 135 deselected**.
- `pytest -q tests/test_ble_interface_core.py tests/test_ble_lifecycle_receive_targets.py tests/test_ble_utils_service_targets.py tests/test_ble_connection_discovery_client_targets.py` -> **264 passed**.
- `pytest -q` (full repo) currently blocked in this environment by optional missing dependency (`pyarrow`) during slog test collection; unrelated to BLE pass changes.
