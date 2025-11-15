# BLE BLE-1114-1 Implementation Plan (Temporary)

> Working branch: `ble-1114-1` (forked from `master`)
>
> Reference implementations:
> - `ble-refine` → stable, practical behavior used daily within mmrelay
> - `ble-refine-dev` → experimental architecture with richer modularity but unresolved stability gaps

## Current Findings

### Master
- BLE implementation is single-threaded aside from a single read loop and lacks explicit state management.
- Reconnection strategy is minimal: a disconnection tears down the interface and requires a restart.
- BLEClient wrapper does not expose timeouts or context for bleak operations and logs "No address provided" when scanning.
- Dependencies still list `bleak >=0.22.3`; the runtime virtualenv confirms 0.22.3 is installed.
- `mesh_interface.py` is synchronous, lightly typed, and lacks defensive parsing/logging guards requested by mmrelay.

### `ble-refine`
- Introduces `BLEStateManager`, `ThreadCoordinator`, `BLEErrorHandler`, and improved auto-reconnect with proper teardown.
- Adds sanitized address handling, fallback discovery for already-connected devices, and consistent event-driven reads.
- Logging shows benign but noisy warnings (`Exceeded max retries for empty BLE read`); functionality is reliable.
- Integrates docstring-rich, defensive parsing in `mesh_interface.py`.

### `ble-refine-dev`
- Pushes toward desired architecture: NotificationManager, RetryPolicy / ReconnectPolicy abstractions, Discovery strategies, Connection orchestrators, reconnection workers, etc.
- Adds pytest infrastructure (fixtures, retry policy tests) and reorganizes responsibilities across helper classes.
- Still unstable in mmrelay; reconnection logic stalls (likely due to aggressive modularization and more async hops).
- Locks `bleak` to 1.1.1 in `pyproject.toml`/`poetry.lock` but runtime env is not yet upgraded.

### mmrelay Logs
- Persistent warning: `Exceeded max retries for empty BLE read` but service continues and reconnects correctly.
- Startup shows "No address provided - only discover method will work" because scanner instantiates BLEClient without an address.
- Provided BLE address is respected (connection succeeds); mmrelay-specific config likely passes the address correctly.
- BLE disconnects triggered by mmrelay currently require OS reboot on stock master; `ble-refine` avoids this.

### Bleak Considerations
- Target runtime should move to `bleak 1.1.1`.
- Need to inspect upstream API changes (event loop teardown, new args, connected-device enumeration) to ensure compatibility.
- Local venv is still on 0.22.3; plan includes bumping dependencies + locking to 1.1.1, then re-vendoring if necessary.

## High-Level Goals
1. **Reliability-first BLE implementation**: start from master, port proven mechanisms from `ble-refine` while preparing hooks for the modular architecture in `ble-refine-dev`.
2. **Architecture alignment**: gradually introduce the orchestration layers (NotificationManager, RetryPolicy abstractions) once baseline stability is ensured.
3. **Dependency modernization**: move repository to `bleak 1.1.1`, confirm API compatibility, and update docs / tooling.
4. **Core interface hygiene**: adopt the safer parsing/logging changes from `ble-refine`’s `mesh_interface.py` and ensure BLE-specific callbacks integrate cleanly.
5. **Plan for future stream/tcp parity**: note required touchpoints for `stream_interface.py`, plus eventual modernization for TCP/serial layers.

## Implementation Phases (subject to refinement)

### Phase 0 – Environment + Dependency Prep
- [ ] Verify/upgrade local env to `bleak 1.1.1` (update `pyproject.toml` + regenerate `poetry.lock` if needed).
- [ ] Audit bleak 1.1.1 source for relevant API changes (connect/disconnect semantics, scanner behavior, event loop expectations).
- [ ] Document adapter-specific requirements (dbus-fast 1.83+, pyobjc 10.3+, etc.) from lockfile for future contributors.

### Phase 1 – Port Stable Mechanics from `ble-refine`
- [ ] Introduce `BLEStateManager`, `ThreadCoordinator`, `BLEErrorHandler`, and associated helper constants (timeouts, retry counts).
- [ ] Replace `should_read` flag with event-driven coordination (`read_trigger`, `reconnected_event`) to reduce busy waiting.
- [ ] Implement `_handle_disconnect` unification plus auto-reconnect worker with bounded backoff/jitter.
- [ ] Rework `_receiveFromRadioImpl` to handle empty reads/transient errors gracefully and log at the appropriate level.
- [ ] Update `BLEClient` wrapper to support timeouts, `await_timeout`, explicit event-loop lifecycle management, and `is_connected`.
- [ ] Pull in improved address sanitization + connected-device fallback scanning to reduce pairing friction.

### Phase 2 – Architect for `ble-refine-dev` Compatibility
- [ ] Introduce `NotificationManager` abstraction to track subscriptions (safe resubscribe after reconnect).
- [ ] Factor RetryPolicy/ReconnectionPolicy scaffolding to allow consistent treatment of empty read retries, transient errors, and reconnect loops.
- [ ] Split discovery/validation/orchestration responsibilities (DiscoveryManager, ConnectionValidator, ReconnectWorker) without regressing the stable behavior.
- [ ] Incrementally add pytest coverage adopted in `ble-refine-dev` (fixtures, state manager tests, retry policy tests) ensuring they pass on the new branch.

### Phase 3 – Mesh/Stream Interface Enhancements
- [ ] Port docstring and defensive parsing updates in `mesh_interface.py` (conversion of bytearray to bytes, per-field logging, `DecodeError` guards).
- [ ] Review event publishing order to ensure mmrelay receives consistent notifications; ensure `_handleFromRadio` remains backward compatible.
- [ ] Audit `stream_interface.py` for shared concepts (state flags, logging) and identify where BLE architectural changes can be mirrored later.
- [ ] Draft follow-up tickets for `tcp_interface.py` and `serial_interface.py` modernization so this branch can reference future work without scope creep.

### Phase 4 – Polishing & Observability
- [ ] Re-tune logging noise (e.g., demote expected empty-read retries, include BLE addresses in warnings).
- [ ] Ensure mmrelay-specific metrics/logging hooks remain intact (no regressions in telemetry or node update pathways).
- [ ] Write migration notes summarizing new BLE behavior, bleak requirements, and testing expectations.

## Mesh / Stream Interface Discussion Points
- `mesh_interface.py`: adopt sanitized node handling, consistent dictionary mutations, and richer telemetry logging from `ble-refine`.
- Consider splitting `_handleFromRadio` into smaller helpers (node updates, config responses) for clarity once BLE layer is stable.
- `stream_interface.py`: evaluate replacing `_wantExit` boolean with shared state manager or at least consistent event controls; unify logging style with BLE interface.
- Later modernization for TCP/serial should reuse RetryPolicy + NotificationManager to maintain parity with BLE.

## Outstanding Questions / Notes
- Need confirmation on mmrelay-specific BLE address handling; investigate if scanner fallback or config parsing is injecting whitespace.
- Determine whether bleak 1.1.1 exposes a supported API for enumerating connected devices (current dev branch uses private `_backend`).
- Evaluate whether repeated empty-read warnings stem from mmrelay’s traffic pattern or bleak’s notification timing; may need adaptive thresholds.
- Keep an eye on `protobufs` working-tree change (already present on master) to avoid accidental edits.

_This file is temporary for coordination; update it as milestones are reached or as design decisions change._
