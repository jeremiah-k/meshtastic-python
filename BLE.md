# BLE Integration Guide

This document covers the current BLE implementation, common field pitfalls, and
recommended patterns for code that embeds `meshtastic-python`.

---

## Architecture overview

| Component                                | Responsibility                                                                                |
| ---------------------------------------- | --------------------------------------------------------------------------------------------- |
| `BLEInterface`                           | User-facing entry point; extends `MeshInterface` with BLE lifecycle management                |
| `BLEClient`                              | Synchronous wrapper around Bleak; delegates async calls to the singleton runner               |
| `BLECoroutineRunner`                     | Process-wide singleton with one background thread and one asyncio event loop                  |
| `BLEStateManager`                        | Centralized state machine (`DISCONNECTED → CONNECTING → CONNECTED → DISCONNECTING`)           |
| `BLEErrorHandler`                        | Unified exception-handling helpers used across the BLE subsystem                              |
| `NotificationManager`                    | Tracks active GATT notification subscriptions so they can be resubscribed after reconnects    |
| `BLENotificationDispatcher`              | Owns notification safety wrappers, FROMNUM parsing, malformed-counter logic, and notify registration |
| `DiscoveryManager`                       | Scan-based BLE device discovery with address normalization                                    |
| `ConnectionValidator`                    | Enforces connection preconditions before any lock is acquired                                 |
| `ClientManager`                          | Owns `BLEClient` lifecycle and safe-close operations                                          |
| `ConnectionOrchestrator`                 | Coordinates a full connection attempt: validate → discover → connect → register notifications |
| `ReconnectScheduler` / `ReconnectWorker` | Policy-driven background reconnect loop                                                       |
| `BLEManagementCommandHandler`            | Instance-bound management collaborator for pair/unpair/trust and temporary-client handling    |
| `BLELifecycleController`                 | Instance-bound lifecycle facade; delegates lifecycle domains to dedicated coordinators          |
| `BLEReceiveLifecycleCoordinator`         | Owns receive intent and receive-thread lifecycle start policy                                  |
| `BLEDisconnectLifecycleCoordinator`      | Owns disconnect target resolution, side effects, and auto-reconnect scheduling                 |
| `BLEConnectionOwnershipLifecycleCoordinator` | Owns verified-connect publication, stale-client cleanup, and gate finalization             |
| `BLEShutdownLifecycleCoordinator`        | Owns close/shutdown sequencing and terminal cleanup orchestration                              |
| `BLEReceiveRecoveryController`           | Instance-bound receive collaborator for read-loop execution, retry, and recovery              |
| `BLECompatibilityEventPublisher`         | Instance-bound publisher for legacy status events and publish-queue drain/flush               |
| `*...Service` classes                    | Compatibility shim surfaces and shared helper logic behind collaborator-owned runtime flows    |

### Boundary contract (current)

- BLEInterface delegates lifecycle/receive/management/compatibility runtime
  paths through bound collaborator instances.
- Lifecycle runtime ownership is partitioned under `BLELifecycleController`
  into receive, disconnect, connection-ownership, and shutdown coordinators.
- Underscore-prefixed methods are canonical for internal orchestration
  collaborators (`state`, `coordination`, lifecycle services).
- `BLEInterface` uses collaborator APIs for state/notification interactions
  (for example `NotificationManager.get_callback()/subscribe()` and
  `BLEStateManager.current_state/is_connected/is_closing/can_connect`) instead
  of direct collaborator-private member reach-through.
- Notification execution/registration paths are owned by
  `BLENotificationDispatcher`; `BLEInterface` exposes compatibility wrappers
  (`_register_notifications`, `_from_num_handler`,
  `_report_notification_handler_error`, `_invoke_safe_execute_compat`) that
  delegate to dispatcher-owned logic.
- Management command orchestration routes through
  `BLEManagementCommandHandler`; interface-level management helpers are facade
  shims that delegate to the collaborator.
- `BLEInterface` remains the compatibility boundary: patch-sensitive
  collaborators (for example `publishingThread`, `BLEClient`,
  `_is_currently_connected_elsewhere`, `sys/shutil/subprocess`) are delegated
  from the interface so existing tests/integrations that monkeypatch
  `meshtastic.interfaces.ble.interface.*` continue to work.
- Service classes remain implementation details; runtime orchestration should
  enter through collaborator/controller interfaces rather than static helpers.
- `BLELifecycleService._*` methods are retained as compatibility/test shim
  entrypoints and delegate to coordinator-owned implementations.

### Key design choices

- **Process-wide address gate.** A registry in
  [`meshtastic/interfaces/ble/gating.py`](meshtastic/interfaces/ble/gating.py)
  prevents two live interfaces from connecting to the same normalized BLE
  address at the same time. Stale (dead-owner) claims are pruned automatically
  after `BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS` (default 300 s).
  A suppressed attempt logs `Connection suppressed: recently connected elsewhere`.

- **Short direct connect, then scan fallback.** A direct connect is tried first
  (see `DIRECT_CONNECT_TIMEOUT_SECONDS` in
  [`meshtastic/interfaces/ble/connection.py`](meshtastic/interfaces/ble/connection.py),
  default 12 s). On failure, a 10 s scan runs and then a full connect uses
  `BLEConfig.CONNECTION_TIMEOUT` (default 60 s).

- **Serialized disconnect handling.** A per-interface `_disconnect_lock`
  deduplicates concurrent disconnect callbacks and marks the address as free
  before scheduling any optional auto-reconnect.

- **Singleton BLE event loop.** All Bleak I/O goes through one shared
  `BLECoroutineRunner`: N clients share 1 background thread, which lowers
  resource usage and simplifies teardown.

- **Optional pairing and Linux trust helper.** `BLEInterface` can request pairing
  during connect (`pair_on_connect=True` or `connect(pair=True)`), and also
  exposes an explicit Linux trust helper (`trust(address=None)`) that calls
  `bluetoothctl trust <addr>`.

---

## Locking rules

When code paths must hold multiple BLE locks, always acquire in this order to
prevent deadlocks:

1. Per-address lock (`_addr_lock_context` in
   [`meshtastic/interfaces/ble/gating.py`](meshtastic/interfaces/ble/gating.py))
2. Global registry lock (`_REGISTRY_LOCK` in the same module) only for short,
   non-blocking registry updates
3. Interface connect lock (`_connect_lock`)
4. Interface management lock (`_management_lock`)
5. Interface state lock (`_state_lock`)
6. Interface disconnect lock (`_disconnect_lock`)

Do not hold `_REGISTRY_LOCK` while blocking on `_addr_lock_context(...)`; the
gating utilities are written to avoid that inversion.

**Exception:** `_handle_disconnect()` acquires `_disconnect_lock` _first_ in
non-blocking mode. If another disconnect handler is already active, the method
returns immediately rather than waiting, which prevents lock inversion while
still deduplicating callbacks.

---

## Internal helper APIs (for library contributors)

These classes are part of the `meshtastic.interfaces.ble` package and are
**not** part of the stable public surface exposed through
`meshtastic.ble_interface`. They are documented here for contributors who
extend or test the BLE subsystem.

### `BLEErrorHandler`

All exception-handling patterns in the BLE subsystem go through this class.

```python
from meshtastic.interfaces.ble.errors import BLEErrorHandler

handler = BLEErrorHandler()

# Execute a callable; return a fallback on any handled exception.
result = handler.safe_execute(
    lambda: risky_call(),
    default_return=None,
    error_msg="risky_call failed",
    reraise=False,
)

# Best-effort cleanup: suppresses all non-exit exceptions, returns bool.
ok = handler.safe_cleanup(lambda: resource.close(), "resource close")
```

`safe_execute` swallows `BleakError`, `DecodeError`, and
`concurrent.futures.TimeoutError`; other exceptions are also caught unless
`reraise=True`. `SystemExit` and `KeyboardInterrupt` always propagate.

Compatibility note: underscore methods (`_safe_execute`, `_safe_cleanup`) are
still available for legacy/internal call sites.

### `NotificationManager`

Tracks GATT notification subscriptions so they can be resubscribed cleanly
after a reconnect. This manager is consumed by
`BLENotificationDispatcher`, which owns notification callback safety and
FROMNUM handler orchestration.

```python
from meshtastic.interfaces.ble.notifications import NotificationManager

mgr = NotificationManager()

# Register a callback for a characteristic UUID; returns an opaque token.
token = mgr.subscribe(uuid, callback)

# Retrieve the most-recently-registered callback for a UUID.
cb = mgr.get_callback(uuid)

# Stop all notifications through a BLEClient (e.g. during shutdown).
mgr.unsubscribe_all(client, timeout=5.0)

# Re-register all subscriptions on a new client (e.g. after reconnect).
mgr.resubscribe_all(client, timeout=5.0)

# Clear internal subscription state (called after full disconnect + cleanup).
mgr.cleanup_all()
```

Compatibility note: underscore methods remain available for legacy/internal
callers; public-first wrappers (`subscribe`, `get_callback`, `unsubscribe_all`,
`resubscribe_all`, `cleanup_all`) are preferred for new code.

### `BLENotificationDispatcher`

Owns notification safety execution and ingress registration policy used by
`BLEInterface`.

```python
from meshtastic.interfaces.ble.notifications import BLENotificationDispatcher

dispatcher = BLENotificationDispatcher(
    notification_manager=notification_manager,
    error_handler_provider=lambda: iface.error_handler,
    trigger_read_event=lambda: iface.thread_coordinator._set_event("read_trigger"),
)

# Register handlers for LOGRADIO/legacy/FROMNUM with safe_execute compatibility probing.
dispatcher.register_notifications(
    iface,
    client,
    legacy_log_handler=iface._legacy_log_radio_handler,
    log_handler=iface._log_radio_handler,
    from_num_handler=iface._from_num_handler,
)

# Delegate malformed FROMNUM accounting and handler error reporting.
dispatcher.handle_malformed_fromnum("Malformed FROMNUM notify")
dispatcher.report_notification_handler_error("Error in FROMNUM notification handler")
```

### `RetryPolicy` / `ReconnectPolicy`

The BLE read loop and auto-reconnect use policies from
[`meshtastic/interfaces/ble/policies.py`](meshtastic/interfaces/ble/policies.py).

Use `RetryPolicy` for bounded retry decisions in the receive/read paths.

```python
from meshtastic.interfaces.ble.policies import RetryPolicy

# Use descriptor presets that return fresh policy instances (preferred):
policy = RetryPolicy.EMPTY_READ  # TRANSIENT_ERROR / AUTO_RECONNECT
# or use factory methods:
policy = RetryPolicy._empty_read()  # or ._transient_error() / ._auto_reconnect()

delay = policy._get_delay(attempt)         # float, jittered exponential backoff
should_go = policy._should_retry(count)    # bool, respects max_retries
```

`ReconnectPolicy` remains an internal BLE policy utility used by reconnect
workers/schedulers:

```python
from meshtastic.interfaces.ble.policies import ReconnectPolicy

policy = ReconnectPolicy(initial_delay=5.0, max_delay=120.0, backoff=2.0, jitter_ratio=0.2)
delay, should_retry = policy.next_attempt()   # compute delay and advance attempt counter
attempt_count = policy.get_attempt_count()    # read current attempt counter
```

Compatibility note: `RetryPolicy` currently exposes underscore factory and
instance methods (`_empty_read`, `_get_delay`, `_should_retry`, etc.) as its
active API surface; these remain required by current internal code and legacy
test doubles.

Compatibility note: the core library does not currently expose camelCase policy
aliases (`emptyRead`, `transientError`, `autoReconnect`) on `RetryPolicy`.
If a downstream wrapper chooses to expose those names, treat them as
compatibility helpers rather than canonical names in
`RetryPolicy` / `ReconnectPolicy`.

For compatibility with existing Python projects, the stable BLE surface exposed
through `meshtastic.ble_interface` keeps the legacy snake_case method names
from the pre-refactor API (for example `find_device`, `read_gatt_char`,
`start_notify`).

Contributor rule for naming updates:

1. Keep legacy snake_case BLE public methods callable.
2. Keep only the approved BLE camelCase promotions callable:
   `findDevice`, `isConnected`, and `stopNotify`.
3. Route compatibility names to one implementation, preferring stable
   canonical helpers.
4. Do not add new BLE aliases unless there is an explicit compatibility need.
5. Do not remove compatibility wrappers unless there is an explicit breaking
   change decision.

---

## Recommended usage

### One interface per device address

```python
from meshtastic.ble_interface import BLEInterface
from pubsub import pub
import time

# Subscribe BEFORE constructing BLEInterface to avoid missing early packets.
pub.subscribe(lambda packet, interface: print(packet), "meshtastic.receive")

# BLEInterface connects automatically during construction.
iface = BLEInterface(address="DD:DD:13:27:74:29")
```

Reuse the same `BLEInterface` instance for the lifetime of the connection to
the device. Creating new instances repeatedly collides with the address gate
and slows recovery.

### Auto-reconnect is opt-in

```python
# Built-in reconnect loop (recommended for long-running integrations):
iface = BLEInterface(address="AA:BB:CC:DD:EE:FF", auto_reconnect=True)

# Manual reconnect (your code drives reconnects instead):
iface = BLEInterface(address="AA:BB:CC:DD:EE:FF", auto_reconnect=False)
# On disconnect: call iface.connect() on the same instance.
```

Do not layer both simultaneously — duplicate reconnect loops produce
`suppressed duplicate connect` log entries and can interfere with the built-in
recovery logic.

### Pairing and trust workflows

Pairing PIN/passkey entry remains OS-agent-driven (for example BlueZ agent /
desktop prompt / platform dialog). Meshtastic can request pairing during
connect, either persistently with `pair_on_connect=True` or for one specific
manual reconnect with `connect(pair=True)`, but it does not replace OS pairing
UX.

```python
from meshtastic.ble_interface import BLEInterface

# Request pairing during every connect attempt.
iface = BLEInterface(
    address="AA:BB:CC:DD:EE:FF",
    pair_on_connect=True,
)

# Use the interface normally after connect/pair completes.
iface.sendText("hello from a paired session")

# Linux-only (requires bluetoothctl): one-time trust setup remains explicit.
iface.trust("AA:BB:CC:DD:EE:FF")
iface.close()
```

```python
# One-time pairing on a specific manual reconnect (with auto_reconnect=False):
iface_manual = BLEInterface(
    address="AA:BB:CC:DD:EE:FF",
    auto_reconnect=False,
)
# Constructor connects immediately; auto_reconnect=False only disables later
# automatic reconnect attempts.
# Later, after the link drops and you want to reconnect with pairing:
iface_manual.connect(pair=True)   # request pairing for this call
iface_manual.sendText("hello after manual reconnect")
# Linux-only (requires bluetoothctl):
iface_manual.trust("AA:BB:CC:DD:EE:FF")
iface_manual.close()
```

Programmatic helpers:

```python
# Pair against the currently connected device. If disconnected but the
# interface already has a resolved target address, BLEInterface may create a
# short-lived temporary client internally to perform the operation.
iface.pair()

# Backend unpair (Linux/Windows backends where supported by Bleak).
iface.unpair()

# Linux-only (requires bluetoothctl) trust helper. When connected, you can omit the address to trust
# the current device. If disconnected, omit the address only when the
# interface already has that device's concrete BLE address bound as its target;
# if the target came from a name lookup or you are unsure, pass the address
# explicitly (no active connection required).
iface.trust()  # Linux-only (requires bluetoothctl)
iface.trust("AA:BB:CC:DD:EE:FF")  # Linux-only (requires bluetoothctl)
```

Platform notes:

- `pair()` and `connect(pair=True)` can trigger OS PIN/passkey prompts; do not assume headless flow.
- `pair()` depends on backend/platform support and may be unavailable on some hosts.
- `unpair()` depends on backend/platform support (typically Linux/Windows).
- `trust(address=None)` is Linux-only and requires `bluetoothctl` in `PATH`.
- On macOS, pairing is OS-managed and explicit `pair()`/`unpair()` are not exposed by the backend.

CLI opt-in:

```bash
meshtastic --ble any --ble-auto-reconnect --listen
```

**When to call `connect()` manually:** the constructor calls it for you. Only
call it again after `close()` or when you are managing reconnects yourself with
`auto_reconnect=False`.

### Respect the address gate

When you see `Connection suppressed: recently connected elsewhere`, another
interface in this process owns the connection. Back off; retries within a few
seconds will continue to be suppressed.

### Keep retries bounded

- The scan + connect cycle can block up to `BLEConfig.CONNECTION_TIMEOUT` (60 s).
  After a failed cycle, wait at least 30–60 s before retrying to avoid
  exhausting Linux/BlueZ resources.
- A scan returning zero devices on Linux is expected when the peripheral is
  already connected elsewhere. Rely on the address gate; repeated scans make
  things worse.

---

## Minimal pubsub example

```python
from meshtastic.ble_interface import BLEInterface
from pubsub import pub
import time

def on_packet(packet, interface):
    print("Packet:", packet)

# Subscribe BEFORE constructing BLEInterface to ensure early packets aren't missed.
pub.subscribe(on_packet, "meshtastic.receive")

# Connection is established in the constructor.
iface = BLEInterface(address="DD:DD:13:27:74:29")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    iface.close()
```

---

## Context-manager pattern

`BLEInterface` supports the context-manager protocol and is the recommended way
to ensure `close()` is always called:

```python
with BLEInterface(address="DD:DD:13:27:74:29") as iface:
    iface.sendText("hello")
# close() is called automatically on any exit, including exceptions.
```

---

## Log interpretation quick reference

| Message                                                    | Meaning                                                                                                               |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `Ignoring disconnect … while a connection is in progress.` | Benign stale callback during CONNECTING; discard.                                                                     |
| `Connection suppressed: recently connected elsewhere`      | Per-address gate blocked duplicate attempt; back off.                                                                 |
| `Cannot connect while interface is closing`                | Interface is mid-shutdown; wait and retry with the same instance.                                                     |
| `Throttling BLE receive recovery: waiting Xs before retry` | Receive thread crashed repeatedly; exponential backoff active.                                                        |
| `BLE receive thread did not exit within Xs`                | Thread took longer than `RECEIVE_THREAD_JOIN_TIMEOUT` (2 s) to exit; non-fatal, but worth investigating for hung I/O. |

---

## Common pitfalls

| Pitfall                                       | Fix                                                                                                                               |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Multiple `BLEInterface` instances per address | Reuse one instance; multiple instances collide on the address gate.                                                               |
| Layered reconnect loops                       | Use either the library's `auto_reconnect=True` **or** your own loop, never both.                                                  |
| Aggressive retry cadence                      | Include exponential backoff; long `scan + connect` calls during rapid retries exhaust BlueZ.                                      |
| Forgetting to resubscribe notifications       | Use the same instance so `NotificationManager` can call `_resubscribe_all()` automatically after reconnects.                      |
| Not closing the interface                     | Always call `close()` or use the context-manager pattern; unclosed BLE handles on Linux prevent future connections (BlueZ quirk). |

---

## When to restart the process

Restart if you observe:

- repeated `Cannot connect while interface is closing` for several minutes, or
- repeated scan + connect cycles timing out on Linux/BlueZ despite the device
  being powered and advertising.

A clean restart clears leaked BLE/DBus handles and resets the process-wide
address gate.
