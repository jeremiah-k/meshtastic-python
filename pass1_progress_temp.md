# BLE Pass 1 Progress (Temporary)

> Temporary architect-facing progress log for the AGOR program.
> Last updated: 2026-03-17
> Status: **PASS 1 complete**

## Pass Objective

Complete BLE ownership transfer by moving runtime authority out of static
service namespaces and into instance-bound collaborators while preserving
behavior and compatibility.

## Completed in This Tranche

- Finalized lifecycle ownership split in `meshtastic/interfaces/ble/lifecycle_service.py`.
- Added/used concrete lifecycle collaborators:
  - `BLEReceiveLifecycleCoordinator`
  - `BLEDisconnectLifecycleCoordinator`
  - `BLEConnectionOwnershipLifecycleCoordinator`
  - `BLEShutdownLifecycleCoordinator`
- Converted major static lifecycle entrypoints into compatibility wrappers that
  delegate to collaborator-owned implementations:
  - disconnect target resolution/side effects
  - verified connect publication and stale-client discard
  - gate finalization and ownership checks
  - close/shutdown orchestration
- Preserved compatibility interception points for monkeypatched test paths by
  retaining static shim surfaces and routing them through collaborator logic.
- Kept runtime behavior stable while reducing centralized authority in
  `BLEInterface` and static lifecycle service cores.

## Validation Evidence

- `ruff check meshtastic/interfaces/ble/lifecycle_service.py meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/management_service.py meshtastic/interfaces/ble/notifications.py` -> passed.
- `poetry run pytest -q tests/test_ble_interface_core.py tests/test_ble_utils_service_targets.py tests/test_ble_lifecycle_receive_targets.py tests/test_ble_connection_discovery_client_targets.py` -> **264 passed**.
- `poetry run pytest -q` -> **1454 passed, 3 skipped, 92 deselected**.

## Current Architectural Shape

- `BLEInterface` delegates runtime domains to bound collaborators.
- Static service methods are now compatibility shims rather than primary
  runtime authority.
- Lifecycle concerns are explicitly partitioned by responsibility
  (receive, disconnect, connection ownership, shutdown).

## Follow-On (Next Pass Candidate)

- PASS 2: further `BLEInterface` decomposition and static-helper reduction in
  remaining non-lifecycle service modules.
- After BLE stabilization: `MeshInterface` dispatch decomposition and
  `Node.setURL()` transaction extraction.
