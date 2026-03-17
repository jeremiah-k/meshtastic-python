# BLE Pass 1 Progress (Temporary)

> Temporary architect-facing progress log for AGOR PASS 1.
> Last updated: 2026-03-17
> Status: **PASS 1 complete (runtime ownership transfer landed)**

## Scope (This Completion Tranche)

- Finish remaining BLE hybrid ownership gaps in lifecycle, receive/recovery, and management command execution.
- Keep public behavior and compatibility unchanged.

## Runtime Authority Retired

- `BLELifecycleService._close`
  - Runtime owner now: `BLEShutdownLifecycleCoordinator.close()` via `BLELifecycleController.close()`.
  - Compatibility shim remains: `BLELifecycleService._close(...)` delegates to shutdown coordinator.
- `BLEReceiveRecoveryService._recover_receive_thread`
  - Runtime owner now: `BLEReceiveRecoveryController.recover_receive_thread()`.
  - Compatibility shim remains: `BLEReceiveRecoveryService._recover_receive_thread(...)` delegates to controller.
- `BLEReceiveRecoveryService._receive_from_radio_impl`
  - Runtime owner now: `BLEReceiveRecoveryController.receive_from_radio_impl()`.
  - Compatibility shim remains: static service method retained for direct compatibility/test invocation.
- `BLEManagementCommandsService._execute_management_command`
  - Runtime owner now: `BLEManagementCommandHandler.execute_management_command()`.
  - Compatibility shim remains: static service implementation retained for direct service-level compatibility tests.

## Additional Ownership Hardening

- `BLEReceiveRecoveryController` now owns runtime read-loop behavior:
  - wait/trigger processing
  - client-state gating
  - payload-read flow
  - transient/fatal recovery decisions
  - restart backoff orchestration
- Receive controller now routes lifecycle actions through collaborator ownership and uses compatibility fallbacks only when tests/fixtures provide legacy iface-level overrides.
- `BLEManagementCommandHandler.execute_management_command()` now owns startup/target/gate/client execution flow directly, with explicit support for instance monkeypatch overrides used by compatibility tests.

## Validation Evidence

- `ruff check meshtastic/interfaces/ble/lifecycle_service.py meshtastic/interfaces/ble/management_service.py meshtastic/interfaces/ble/receive_service.py tests/test_ble_interface_core.py` -> passed.
- `poetry run pytest -q tests/test_ble_interface_core.py tests/test_ble_utils_service_targets.py tests/test_ble_lifecycle_receive_targets.py tests/test_ble_connection_discovery_client_targets.py` -> **264 passed**.
- `poetry run pytest -q` -> **1454 passed, 3 skipped, 92 deselected**.

## Remaining Static-Core Hotspots

- Some static service methods remain intentionally non-trivial in `receive_service.py` and `management_service.py` for direct compatibility/test entrypoints.
- These are no longer the primary runtime authority for `BLEInterface` call paths, but they still exist as compatibility surfaces.
