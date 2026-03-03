<!-- trunk-ignore-all -->
<!-- markdownlint-disable -->

# CodeRabbit IDE/Linting Comments - Actionable Items

> Total items: 113
> Generated: 2026-03-03T13:02:45.337961

## Summary by Severity

| Severity | Count |
| -------- | ----- |
| Minor    | 5     |
| Nitpick  | 108   |

## MINOR (5 items)

### `tests/tuntest.py:111`

**Type:** actionable

**Missing bounds check for UDP/TCP port extraction.**

The code reads ports at offsets `subheader` and `subheader + 2` without verifying the packet is long enough. A malformed packet could cause an `IndexError` when accessing port bytes.

<details>
<summary>Proposed fix for UDP handling</summary>

```diff
         elif protocol == 0x11:  # UDP
+            if len(p) < subheader + 4:
+                logging.debug("Ignoring malformed UDP packet: too short for ports")
+                continue
             srcport = readnet_u16(p, subheader)
             destport = readnet_u16(p, subheader + 2)
```

</details>

<details>
<summary>Proposed fix for TCP handling</summary>

```diff
         elif protocol == 0x06:  # TCP
+            if len(p) < subheader + 4:
+                logging.debug("Ignoring malformed TCP packet: too short for ports")
+                continue
             srcport = readnet_u16(p, subheader)
             destport = readnet_u16(p, subheader + 2)
```

</details>

---

### `tests/test_ble_connection_edge_cases.py:277`

**Type:** actionable

**Good test for interrupt handling; add trailing newline.**

The test correctly verifies that `KeyboardInterrupt` during connection properly resets state and closes the client. However, the file is missing a trailing newline at the end (PEP 8).

<details>
<summary>🔧 Add trailing newline</summary>

```diff
     assert state_manager._current_state == ConnectionState.DISCONNECTED
     client_manager._safe_close_client.assert_called_once_with(mock_client)
+
```

</details>

**Suggestions:**

```text
@pytest.mark.unit
def test_connection_orchestrator_interrupt_resets_state_and_closes_client() -> None:
    """Interrupts during connect should reset state to DISCONNECTED and close client."""
    state_manager = BLEStateManager()
    state_lock = RLock()
    validator = ConnectionValidator(state_manager, state_lock, MockBLEError)

    client_manager = MagicMock()
    mock_client = MagicMock()
    client_manager._create_client.return_value = mock_client
    client_manager._connect_client.side_effect = KeyboardInterrupt()

    interface = MagicMock()
    interface.BLEError = MockBLEError

    orchestrator = ConnectionOrchestrator(
        interface=interface,
        validator=validator,
        client_manager=client_manager,
        discovery_manager=MagicMock(),
        state_manager=state_manager,
        state_lock=state_lock,
        thread_coordinator=MagicMock(),
    )

    with pytest.raises(KeyboardInterrupt):
        orchestrator._establish_connection(
            address="AA:BB:CC:DD:EE:FF",
            current_address=None,
            register_notifications_func=lambda _client: None,
            on_connected_func=lambda: None,
            on_disconnect_func=lambda _client: None,
        )

    assert state_manager._current_state == ConnectionState.DISCONNECTED
    client_manager._safe_close_client.assert_called_once_with(mock_client)
```

---

### `meshtastic/mesh_interface.py:414`

**Type:** actionable

**Potential `KeyError` if node lacks `user` dictionary.**

Line 415 checks `"macaddr" in n2["user"]` and line 422 accesses `n2["user"]["id"]`, but neither verifies that `"user"` exists in `n2` first. If a node entry lacks a `"user"` key, this will raise a `KeyError`.

<details>
<summary>Proposed fix</summary>

```diff
-            # if we have 'macaddr', re-format it
-            if "macaddr" in n2["user"]:
-                val = n2["user"]["macaddr"]
-                # decode the base64 value
-                addr = convert_mac_addr(val)
-                n2["user"]["macaddr"] = addr
-
-            # use id as dictionary key for correct json format in list of nodes
-            nodeid = n2["user"]["id"]
-            nodes[nodeid] = n2
+            # if we have 'macaddr', re-format it
+            if "user" in n2:
+                if "macaddr" in n2["user"]:
+                    val = n2["user"]["macaddr"]
+                    # decode the base64 value
+                    addr = convert_mac_addr(val)
+                    n2["user"]["macaddr"] = addr
+
+                # use id as dictionary key for correct json format in list of nodes
+                if "id" in n2["user"]:
+                    nodeid = n2["user"]["id"]
+                    nodes[nodeid] = n2
```

</details>

---

### `codecov.yml:1`

**Type:** actionable

**Potential conflict: Global project target exceeds some component targets.**

The global project target is set to 70%, but several components below have lower targets (30%, 40%, 50%, 60%). Codecov will evaluate both global and component-level rules, which could lead to confusion about which target applies. Ensure that the lower component targets won't cause the global 70% target to fail, or consider adjusting the global target to align with your component-specific goals.

---

### `Makefile:47`

**Type:** actionable

**Minor portability concern with hardcoded `/tmp` path.**

The `PYLINTHOME=/tmp/pylint-cache` assumes a Unix-like environment. If this project needs Windows support, consider using a more portable approach.

<details>
<summary>Suggested portable alternative</summary>

```diff
-	PYLINTHOME=/tmp/pylint-cache $(POETRY_RUN) pylint meshtastic examples/ --ignore-patterns ".*_pb2\\.pyi?$$"
+	PYLINTHOME=$${TMPDIR:-/tmp}/pylint-cache $(POETRY_RUN) pylint meshtastic examples/ --ignore-patterns ".*_pb2\\.pyi?$$"
```

This uses `$TMPDIR` if set (common on macOS/Linux), falling back to `/tmp`. For full Windows compatibility, you might need a different approach or accept this as a Unix-only target.

</details>

**Suggestions:**

```text
\# lint the codebase (same command as CI)
lint:
	PYLINTHOME=$${TMPDIR:-/tmp}/pylint-cache $(POETRY_RUN) pylint meshtastic examples/ --ignore-patterns ".*_pb2\\.pyi?$$"
```

---

## NITPICK (108 items)

### `tests/tuntest.py:85`

**Type:** nitpick

**Redundant assignment before `continue`.**

Setting `ignore = True` on line 87 has no effect because `continue` immediately skips to the next loop iteration. Remove the redundant assignment.

<details>
<summary>Proposed fix</summary>

```diff
             if len(p) <= icmp_offset:
                 logging.debug("Ignoring malformed ICMP packet: missing type byte")
-                ignore = True
                 continue
```

</details>

---

### `tests/tuntest.py:43`

**Type:** nitpick

**Naming convention: consider making this an internal function.**

Consider renaming to `_readnet_u16` to match internal function naming conventions, or `readNetU16` if intended as public API.

<details>
<summary>Proposed rename to internal function</summary>

```diff
-def readnet_u16(p: bytes | bytearray | memoryview, offset: int) -> int:
+def _readnet_u16(p: bytes | bytearray | memoryview, offset: int) -> int:
     """Read big endian u16 (network byte order)."""
     return p[offset] * 256 + p[offset + 1]
```

</details>

---

### `tests/tuntest.py:38`

**Type:** nitpick

**Naming convention: consider making this an internal function.**

Same as `hexstr` above—consider renaming to `_ipstr` for consistency with internal function naming conventions.

<details>
<summary>Proposed rename to internal function</summary>

```diff
-def ipstr(barray: bytes | bytearray) -> str:
+def _ipstr(barray: bytes | bytearray) -> str:
     """Print a string of ip digits."""
     return ".".join("{}".format(x) for x in barray)
```

</details>

---

### `tests/tuntest.py:33`

**Type:** nitpick

**Naming convention: consider making this an internal function.**

Per coding guidelines, public functions should use `camelCase` (e.g., `hexStr`), while internal functions should use `snake_case` with a leading underscore. Since this appears to be a helper function, consider renaming to `_hexstr`.

<details>
<summary>Proposed rename to internal function</summary>

```diff
-def hexstr(barray: bytes | bytearray) -> str:
+def _hexstr(barray: bytes | bytearray) -> str:
     """Print a string of hex digits."""
     return ":".join("{:02x}".format(x) for x in barray)
```

</details>

---

### `tests/test_ble_state_manager.py:51`

**Type:** nitpick

**Direct state mutation bypasses transition validation — intentional but tightly coupled.**

Directly assigning `manager._state = ConnectionState.X` tests properties in isolation but couples the test to internal implementation details. If the internal attribute name changes, these tests break silently.

Consider whether using `_transition_to()` with appropriate state sequences would be more robust while still testing the same property behaviors. That said, if the intent is specifically to test properties independently of transition logic, this approach is acceptable.

---

### `tests/test_ble_runner.py:450`

**Type:** nitpick

**Stub docstrings are overly verbose and potentially misleading.**

The docstrings on `FakeLoop` and `FakeThread` describe real implementation behavior rather than stub behavior. For test stubs, simpler docstrings explaining their test purpose would be clearer:

<details>
<summary>♻️ Simplified stub example</summary>

```python
class FakeLoop:
    """Loop stub that never stops for testing zombie timeout behavior."""

    def is_running(self) -> bool:
        """Always returns True to simulate a loop that won't stop."""
        return True

    def stop(self) -> None:
        """No-op stop for testing."""
        pass

    def call_soon_threadsafe(self, fn):
        """Execute fn synchronously."""
        fn()
```

</details>

---

### `tests/test_ble_runner.py:208`

**Type:** nitpick

**Consider extracting repeated test helpers to reduce duplication.**

The `_LoopStub` class, `_fake_submit` function, and `_noop` coroutine are duplicated across multiple tests (lines 208-249, 268-309, 329-370, 385-387). Extracting these to module-level helpers or fixtures would improve maintainability.

<details>
<summary>♻️ Proposed refactor: extract common helpers</summary>

Add at module level (after imports):

```python
class _LoopStub:
    """Loop stub that always reports a running event loop."""

    @staticmethod
    def is_running() -> bool:
        return True


def _fake_submit(coro, _loop) -> Future:
    """Close coroutine and return completed Future with None result."""
    coro.close()
    future: Future[None] = Future()
    future.set_result(None)
    return future


async def _noop() -> None:
    """No-op coroutine for testing."""
    return None
```

Then reference these in the affected tests instead of redefining them inline.

</details>

---

### `tests/test_ble_runner.py:146`

**Type:** nitpick

**Add missing return type hints to helper functions.**

Several helper functions are missing return type annotations, e.g., `_capture_error` should be `-> None`. Similarly, `never_complete` (line 168), `dummy` (lines 621, 645), and async `disconnect` methods (lines 683, 710) could benefit from explicit return types.

<details>
<summary>♻️ Example fix</summary>

```diff
-        def _capture_error(_message: str, *args: Any, **kwargs: Any) -> None:
+        def _capture_error(_message: str, *args: Any, **kwargs: Any) -> None:
             _ = args
             observed_exc_info.append(kwargs.get("exc_info"))
```

(Note: this particular one already has `-> None`, but apply similar pattern to lines 168, 621, 645, 683, 710)

</details>

---

### `tests/test_ble_interface_fixtures.py:584`

**Type:** nitpick

**Minor docstring inaccuracy.**

The docstring refers to "module-level `connect_calls`" but it's actually a closure-captured local variable from the enclosing `_build_interface` function.

<details>
<summary>📝 Suggested fix</summary>

```diff
-        Appends `_address` to the `connect_calls` list, sets `_self.client` to the prepared test client, clears `_self._disconnect_notified`, and triggers `_self._reconnected_event.set()` if `_reconnected_event` exists.
+        Appends `_address` to the closure-captured `connect_calls` list, sets `_self.client` to the prepared test client, clears `_self._disconnect_notified`, and triggers `_self._reconnected_event.set()` if `_reconnected_event` exists.
```

</details>

---

### `tests/test_ble_interface_fixtures.py:483`

**Type:** nitpick

**Incomplete docstring Parameters section.**

The parameters are not properly formatted in NumPy style. Each parameter should have its own entry with name and type.

<details>
<summary>📝 Suggested docstring fix</summary>

```diff
     Parameters
     ----------
-        pytest.MonkeyPatch
-        Fixture used to apply the attribute patches.
-        Fixtures accepted solely to enforce fixture ordering; not otherwise used.
+    monkeypatch : pytest.MonkeyPatch
+        Fixture used to apply the attribute patches.
+    mock_serial, mock_pubsub, mock_tabulate, mock_bleak, mock_bleak_exc, mock_publishing_thread : types.ModuleType
+        Fixtures accepted solely to enforce fixture ordering; not otherwise used.
```

</details>

---

### `tests/test_ble_interface_fixtures.py:232`

**Type:** nitpick

**Minor docstring inconsistency.**

The docstring describes the general contract but doesn't clarify this stub always returns `False`. Consider updating for clarity.

<details>
<summary>📝 Suggested docstring fix</summary>

```diff
         def is_connected(self):
             """Report whether the client is connected.

-            This dummy implementation always reports the client as disconnected.
+            This stub always returns False to simulate a disconnected state.

             Returns
             -------
             bool
-                `True` if the client is connected, `False` otherwise.
+                Always `False` (stub simulates disconnected state).
             """
             return False
```

</details>

---

### `tests/test_ble_interface_core.py:413`

**Type:** nitpick

**Consider adding type hints to fixture parameters.**

The `monkeypatch` parameter is missing a type hint. While test functions commonly omit return type hints, adding `monkeypatch: pytest.MonkeyPatch` improves IDE support and consistency with other tests in this file.

<details>
<summary>💡 Suggested improvement</summary>

```diff
-def test_handle_disconnect_ignores_stale_callbacks(monkeypatch):
+def test_handle_disconnect_ignores_stale_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
```

</details>

---

### `tests/test_ble_interface_advanced.py:788`

**Type:** nitpick

**Consider consolidating duplicate `_FakeFuture` classes.**

Both `test_ble_client_async_timeout_maps_to_ble_error` and `test_ble_client_async_runtime_error_maps_to_ble_error` define nearly identical `_FakeFuture` classes. The only difference is the exception raised in `result()`. This could be refactored using a parameterized test or a shared factory.

<details>
<summary>Sketch of parameterized approach</summary>

```python
def _make_fake_future(exception_factory):
    """Create a _FakeFuture that raises the given exception in result()."""
    class _FakeFuture:
        def __init__(self):
            self.cancelled = False
            self.coro = None
            self.callbacks: list[Callable[..., Any]] = []

        def result(self, _timeout=None):
            raise exception_factory()

        def cancel(self):
            self.cancelled = True

        def add_done_callback(self, callback):
            self.callbacks.append(callback)

    return _FakeFuture()


@pytest.mark.parametrize(
    "exception_factory,expected_msg",
    [
        (FutureTimeoutError, "Async operation timed out"),
        (lambda: RuntimeError("loop is closed"), "Async operation failed: loop is closed"),
    ],
)
def test_ble_client_async_error_mapping(monkeypatch, exception_factory, expected_msg):
    ...
```

</details>

---

### `tests/test_ble_interface_advanced.py:204`

**Type:** nitpick

**Incomplete docstring for `**kwargs` parameter.\*\*

The `**kwargs` parameter description is malformed. The description text appears on line 211 but isn't associated with a properly formatted parameter entry.

<details>
<summary>Suggested fix</summary>

```diff
         def _capture_events(topic, **kwargs):
             """Record a pub/sub event for test inspection.

             Parameters
             ----------
             topic : Any
                 The event topic name.
-                Key/value pairs comprising the event payload; stored as a dict.
+            **kwargs : Any
+                Key/value pairs comprising the event payload; stored as a dict.

             Notes
             -----
```

</details>

---

### `tests/test_ble_interface_advanced.py:185`

**Type:** nitpick

**Consider removing unused `caplog` fixture.**

The `caplog` fixture is explicitly marked as unused on line 198 and isn't used for any assertions in this test. If it's not needed, remove it from the function signature to reduce noise.

<details>
<summary>Suggested change</summary>

```diff
-def test_auto_reconnect_behavior(monkeypatch, caplog):
+def test_auto_reconnect_behavior(monkeypatch):
     """Verify BLEInterface schedules an auto-reconnect after a BLE disconnect and preserves connection state and receive-thread behavior.
     ...
     Uses test fixtures for monkeypatching and logging; does not document those fixtures.
     """
-    _ = caplog  # Mark as unused
```

</details>

---

### `tests/test_ble_interface_advanced.py:7`

**Type:** nitpick

**Consider importing `Callable` from `collections.abc` for Python 3.10+ consistency.**

`Iterator` is already imported from `collections.abc` (line 7). For consistency with the Python 3.10+ baseline, `Callable` should also come from `collections.abc` rather than `typing`.

<details>
<summary>Suggested change</summary>

```diff
-from collections.abc import Iterator
+from collections.abc import Callable, Iterator
 from concurrent.futures import TimeoutError as FutureTimeoutError
 from contextlib import ExitStack, contextmanager
 from queue import Queue
-from typing import Any, Callable, cast
+from typing import Any, cast
```

</details>

---

### `tests/test_ble_integration_scenarios.py:232`

**Type:** nitpick

**Good cleanup pattern, but consider test isolation.**

The `try/finally` block properly restores `BLEConfig.CONNECTION_TIMEOUT`. However, if tests run in parallel (e.g., with `pytest-xdist`), modifying global state could cause flaky behavior. Consider using a pytest fixture with proper scoping or monkeypatch for better isolation.

<details>
<summary>♻️ Suggested refactor using monkeypatch</summary>

```diff
 @pytest.mark.unit
-def test_ble_config_can_be_modified_at_runtime() -> None:
+def test_ble_config_can_be_modified_at_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
     """Test that BLEConfig attributes can be modified for testing/tuning."""
-    original_timeout = BLEConfig.CONNECTION_TIMEOUT
-
-    try:
-        # Should be able to modify
-        BLEConfig.CONNECTION_TIMEOUT = 120.0
-        assert BLEConfig.CONNECTION_TIMEOUT == 120.0
-
-        # Module-level alias won't change (it's a snapshot)
-        # but __getattr__ should return the new value
-        from meshtastic.interfaces.ble import constants
-
-        via_getattr = constants.CONNECTION_TIMEOUT
-        # via_getattr might be the old snapshot value, which is expected
-        assert isinstance(via_getattr, float)
-    finally:
-        # Restore
-        BLEConfig.CONNECTION_TIMEOUT = original_timeout
+    # Should be able to modify
+    monkeypatch.setattr(BLEConfig, "CONNECTION_TIMEOUT", 120.0)
+    assert BLEConfig.CONNECTION_TIMEOUT == 120.0
+
+    # Module-level alias won't change (it's a snapshot)
+    # but __getattr__ should return the new value
+    from meshtastic.interfaces.ble import constants
+
+    via_getattr = constants.CONNECTION_TIMEOUT
+    # via_getattr might be the old snapshot value, which is expected
+    assert isinstance(via_getattr, float)
```

</details>

---

### `tests/test_ble_gating.py:307`

**Type:** nitpick

**Consider simplifying docstrings on test stub methods.**

The NumPy-style docstrings on the `_is_connection_connected` methods in these test stub classes (lines 311-318, 334-341) are quite verbose for test code. Since these are internal test helpers, simpler inline comments or even no docstrings would suffice.

<details>
<summary>♻️ Simplified version</summary>

```diff
         class Owner:
             """Test owner stub."""

             def _is_connection_connected(self) -> bool:
-                """Report whether the associated connection is currently established.
-
-                Returns
-                -------
-                bool
-                    `True` if the connection is established, `False` otherwise.
-                """
+                """Return False to simulate disconnected state."""
                 return False
```

</details>

---

### `tests/test_ble_connection_edge_cases.py:27`

**Type:** nitpick

**Consider extracting repeated setup to pytest fixtures.**

The `state_manager` and `lock` creation pattern is repeated in every test. Per coding guidelines for test files, consider using `pytest` fixtures from `conftest.py` to reduce boilerplate.

<details>
<summary>♻️ Suggested fixture in conftest.py</summary>

```python
\# In conftest.py or at the top of this file
@pytest.fixture
def state_manager():
    """Provide a fresh BLEStateManager for testing."""
    return BLEStateManager()

@pytest.fixture
def state_lock():
    """Provide a fresh RLock for testing."""
    return RLock()

@pytest.fixture
def connection_validator(state_manager, state_lock):
    """Provide a ConnectionValidator configured with MockBLEError."""
    return ConnectionValidator(state_manager, state_lock, MockBLEError)
```

Then tests become:

```python
@pytest.mark.unit
def test_connection_validator_allows_connect_when_disconnected(connection_validator) -> None:
    """ConnectionValidator should allow connection when state is DISCONNECTED."""
    connection_validator._validate_connection_request()
```

</details>

---

### `tests/test_ble_client_edge_cases.py:19`

**Type:** nitpick

**Add return type hints to test functions.**

Per coding guidelines, all new code should have type hints. Test functions returning nothing should be annotated with `-> None`.

<details>
<summary>♻️ Proposed fix</summary>

```diff
 @pytest.mark.unit
-def test_bleclient_discovery_mode_without_address():
+def test_bleclient_discovery_mode_without_address() -> None:
     """BLEClient should support discovery-only mode when initialized without an address."""
     ...

 @pytest.mark.unit
-def test_bleclient_isConnected_handles_missing_bleak_client():
+def test_bleclient_isConnected_handles_missing_bleak_client() -> None:
     """IsConnected should return False when bleak_client is None."""
     ...

 @pytest.mark.unit
-def test_bleclient_is_connected_alias():
+def test_bleclient_is_connected_alias() -> None:
     """is_connected should be an alias for isConnected."""
     ...
```

</details>

---

### `meshtastic/version.py:11`

**Type:** nitpick

**Consider camelCase naming for public API consistency.**

Per coding guidelines, public functions should use camelCase (e.g., `getActiveVersion`). However, since this appears to be an existing function (signature was updated, not added), renaming would be a breaking change. If you decide to align with the convention in the future, a silent compatibility shim (`COMPAT_STABLE_SHIM`) could preserve backward compatibility.

The implementation logic itself is clean and correct.

---

### `meshtastic/util.py:1149`

**Type:** nitpick

**Naming convention inconsistency: consider renaming or marking as internal.**

The function `detect_windows_port` uses `snake_case` without a leading underscore. Per the coding guidelines:

- Public functions should use `camelCase` (e.g., `detectWindowsPort`)
- Internal functions should use `_snake_case` (e.g., `_detect_windows_port`)

Since this function appears to be used internally by `active_ports_on_supported_devices`, consider renaming to `_detect_windows_port` to mark it as internal.

---

### `meshtastic/util.py:661`

**Type:** nitpick

**Consider adding camelCase primary for consistency (optional, low priority).**

Functions like `remove_keys_from_dict`, `channel_hash`, `generate_channel_hash`, etc. use `snake_case` naming. While the coding guidelines specify `camelCase` for public functions, mass renaming would cause significant churn.

The newer functions in this file (`messageToJson`, `toNodeNum`, `flagsToList`) demonstrate the preferred pattern: `camelCase` primary with a `COMPAT_STABLE_SHIM` snake_case alias. Consider applying this pattern incrementally when these functions are touched in future changes, rather than all at once.

---

### `meshtastic/util.py:277`

**Type:** nitpick

**Consider adding a `COMPAT_STABLE_SHIM` marker or providing a `camelCase` primary.**

The function `our_exit` uses `snake_case` naming, but per the coding guidelines, public functions should use `camelCase`. The docstring mentions this is a "Compatibility helper" for backward compatibility—if that's the case, consider either:

1. Adding a `# COMPAT_STABLE_SHIM` comment to make the compatibility status explicit, or
2. Creating a `camelCase` primary (`ourExit`) with `our_exit` as the shim (consistent with `toNodeNum`/`to_node_num` pattern).

Given this function is explicitly noted as being retained for legacy callers and the recommendation to prefer local CLI helpers, option 1 (just adding the marker) seems more appropriate.

---

### `meshtastic/tunnel.py:483`

**Type:** nitpick

**Add COMPAT_STABLE_SHIM markers to compatibility wrappers.**

Per coding guidelines, historical compatibility shims should be marked with `COMPAT_STABLE_SHIM` to document their purpose and indicate they should remain callable without warnings.

<details>
<summary>Suggested change</summary>

```diff
-    # Backward-compatible aliases for existing callers/tests.
+    # COMPAT_STABLE_SHIM: Backward-compatible aliases for existing callers/tests.
     def _shouldFilterPacket(self, p: bytes) -> bool:
-        """Compatibility wrapper for _should_filter_packet."""
+        """COMPAT_STABLE_SHIM: Compatibility wrapper for _should_filter_packet."""
         return self._should_filter_packet(p)

     def _ipToNodeId(self, ipAddr: bytes) -> str | None:
-        """Compatibility wrapper for _ip_to_node_id."""
+        """COMPAT_STABLE_SHIM: Compatibility wrapper for _ip_to_node_id."""
         return self._ip_to_node_id(ipAddr)

     def _nodeNumToIp(self, nodeNum: int) -> str:
-        """Compatibility wrapper for _node_num_to_ip."""
+        """COMPAT_STABLE_SHIM: Compatibility wrapper for _node_num_to_ip."""
         return self._node_num_to_ip(nodeNum)

     def sendPacket(self, destAddr: bytes, p: bytes) -> None:
-        """Compatibility wrapper for _send_packet."""
+        """COMPAT_STABLE_SHIM: Compatibility wrapper for _send_packet."""
         self._send_packet(destAddr, p)
```

</details>

As per coding guidelines: "Use COMPAT_STABLE_SHIM marker for historical/public compatibility shims that should remain callable and not warn."

---

### `meshtastic/tunnel.py:183`

**Type:** nitpick

**Consider adding COMPAT_STABLE_SHIM markers to legacy attribute aliases.**

These compatibility aliases (`udpBlacklist`, `tcpBlacklist`, `protocolBlacklist`) preserve backward compatibility for external callers. Per coding guidelines, historical compatibility shims should be marked with `COMPAT_STABLE_SHIM` for documentation purposes.

```diff
-        # Legacy compatibility aliases
+        # COMPAT_STABLE_SHIM: Legacy compatibility aliases (camelCase attribute names)
         self.udpBlacklist = self.UDP_BLACKLIST
         self.tcpBlacklist = self.TCP_BLACKLIST
         self.protocolBlacklist = self.PROTOCOL_BLACKLIST
```

---

### `meshtastic/tests/test_version.py:104`

**Type:** nitpick

**Add return type hint to `_fake_get`.**

Same as the previous test, this helper function should include a return type annotation.

<details>
<summary>Proposed fix</summary>

```diff
-    def _fake_get(url: str, timeout: float):
+    def _fake_get(url: str, timeout: float) -> _FakeResponse:
```

</details>

---

### `meshtastic/tests/test_version.py:69`

**Type:** nitpick

**Add return type hint to `_fake_get`.**

The helper function is missing a return type annotation for consistency with the coding guidelines.

<details>
<summary>Proposed fix</summary>

```diff
-    def _fake_get(url: str, timeout: float) -> _FakeResponse:
+    def _fake_get(url: str, timeout: float) -> _FakeResponse:
```

Wait, looking again, the function signature on line 69 actually doesn't have a return type. The fix should be:

```diff
-    def _fake_get(url: str, timeout: float):
+    def _fake_get(url: str, timeout: float) -> _FakeResponse:
```

</details>

---

### `meshtastic/tests/test_util.py:664`

**Type:** nitpick

**Minor naming inconsistency.**

The test name `test_acknowledgement_reset` uses British spelling while the class being tested is `Acknowledgment` (American spelling). Consider renaming to `test_acknowledgment_reset` for consistency with the class name.

<details>
<summary>♻️ Suggested rename</summary>

```diff
 @pytest.mark.unit
-def test_acknowledgement_reset() -> None:
-    """Test that the reset method can set all fields back to False."""
+def test_acknowledgment_reset() -> None:
+    """Test that Acknowledgment.reset() sets all fields back to False."""
```

</details>

---

### `meshtastic/tests/test_tunnel.py:263`

**Type:** nitpick

**Same cleanup pattern issue.**

Tests `test_ip_to_node_id_none` and `test_ip_to_node_id_all` also create tunnels without explicit `close()` calls. Same consistency concern as noted above.

---

### `meshtastic/tests/test_tunnel.py:162`

**Type:** nitpick

**Multiple tests missing explicit `tun.close()`.**

Tests `test_should_filter_packet_in_blacklist`, `test_should_filter_packet_icmp`, `test_should_filter_packet_udp`, `test_should_filter_packet_udp_blacklisted`, `test_should_filter_packet_tcp`, and `test_should_filter_packet_tcp_blacklisted` create `Tunnel` instances without explicit cleanup. While the autouse fixture handles this, the pattern is inconsistent with tests like `test_should_filter_packet_short_header` (line 154-158) which properly use `try/finally`.

Consider standardizing the cleanup pattern across all tests for consistency.

---

### `meshtastic/tests/test_tunnel.py:127`

**Type:** nitpick

**Missing `tun.close()` call.**

The tunnel created on line 138 is not closed. For consistency with other tests and to ensure proper cleanup, consider adding a `try/finally` block.

---

### `meshtastic/tests/test_tunnel.py:108`

**Type:** nitpick

**Consider explicit `Tunnel.close()` for consistency.**

Same as the previous test - the `Tunnel` instance created on line 122 is not explicitly closed. Consider using `try/finally` for consistency with other tests.

---

### `meshtastic/tests/test_tunnel.py:88`

**Type:** nitpick

**Consider explicit `Tunnel.close()` for consistency.**

The `Tunnel` instance created on line 102 is not explicitly closed. While the `reset_tunnel_mt_config_state` autouse fixture handles cleanup, other tests in this file (e.g., `test_Tunnel_with_interface`) explicitly use `try/finally` to close the tunnel. Consider adding the same pattern here for consistency and clarity.

<details>
<summary>♻️ Suggested refactor</summary>

```diff
     with caplog.at_level(logging.DEBUG):
-        Tunnel(iface)
-        onTunnelReceive(packet, iface)
+        tun = Tunnel(iface)
+        try:
+            onTunnelReceive(packet, iface)
+        finally:
+            tun.close()
     assert re.search(r"in onTunnelReceive", caplog.text, re.MULTILINE)
```

</details>

---

### `meshtastic/tests/test_tcp_interface.py:426`

**Type:** nitpick

**Timing-dependent tests may be flaky on slow CI systems.**

These tests use real `time.sleep` and rely on precise thread scheduling. While necessary for testing interruptible sleep behavior, consider:

1. The 10.0s delay (line 444, 473) relies on the background thread triggering within the polling interval.
2. `time.sleep(0.1)` in helper threads (lines 437, 466) adds latency and assumes thread scheduling happens promptly.
3. The 0.3s real delay (line 494) slows down the test suite.

If flakiness occurs in CI, consider:

- Using `pytest.mark.slow` or similar to run these separately
- Reducing the interrupt delay check interval in the implementation to make tests more deterministic
- Using a condition variable or event for synchronization instead of timing-based coordination

---

### `meshtastic/tests/test_supported_device.py:573`

**Type:** nitpick

**Tautological assertion can be simplified.**

The assertion on line 578 is redundant since the list comprehension on line 576 already filters for `device_class == "nrf52"`. The assertion will always pass by definition of the filter.

<details>
<summary>♻️ Suggested simplification</summary>

```diff
 @pytest.mark.unit
 def test_nrf52_device_class() -> None:
     """Test that nrf52 devices have correct device_class."""
     nrf52_devices = [d for d in supported_devices if d.device_class == "nrf52"]
     assert len(nrf52_devices) > 0
-    assert all(d.device_class == "nrf52" for d in nrf52_devices)
```

Alternatively, if the intent is to verify specific known nrf52 devices are included:

```python
@pytest.mark.unit
def test_nrf52_device_class() -> None:
    """Test that nrf52 devices exist in supported_devices."""
    nrf52_devices = [d for d in supported_devices if d.device_class == "nrf52"]
    assert len(nrf52_devices) > 0
    assert rak4631_5005 in nrf52_devices
    assert rak4631_19003 in nrf52_devices
```

</details>

---

### `meshtastic/tests/test_stream_interface.py:277`

**Type:** nitpick

**Consider adding a comment explaining the noProto manipulation.**

The temporary `noProto = False` at line 285 (to exercise `_wait_connected`) and restoration at line 302 (before `close()`) is functional but the reasoning may not be immediately clear to future maintainers.

<details>
<summary>📝 Suggested documentation improvement</summary>

```diff
     iface = StreamInterface(noProto=True, connectNow=False)
     try:
         iface._provides_own_stream = True  # type: ignore[attr-defined]
         iface.stream = None
+        # Temporarily disable noProto to exercise _wait_connected code path
         iface.noProto = False
         iface._rxThread = MagicMock()
         iface._rxThread.is_alive.return_value = False
```

</details>

---

### `meshtastic/tests/test_smokevirt.py:782`

**Type:** nitpick

**Consider documenting the virtual radio limitation.**

The test ends with a note that the virtual radio won't respond well after factory reset. The `# TODO: fix?` comment could be more actionable. Consider either:

1. Adding a `pytest.mark.xfail` or skip marker if this causes subsequent test failures
2. Documenting this as a known limitation in the test docstring
3. Creating a tracking issue for the virtual radio improvement

---

### `meshtastic/tests/test_smokevirt.py:146`

**Type:** nitpick

**Shell redirection pattern may be fragile.**

The command `--qr > {quoted_filename}` uses shell redirection to capture output. This works only if `run_cli_with_timeout` invokes the command with `shell=True`. Consider whether using the helper's return value (`out`) and writing it to the file explicitly would be more robust and portable:

```python
return_value, out = run_cli_with_timeout("meshtastic --host localhost --qr")
filename.write_text(out)
```

This would avoid shell-specific behavior and make the test's intent clearer.

---

### `meshtastic/tests/test_slog_power_logger.py:219`

**Type:** nitpick

**Consider extracting duplicated row extraction logic.**

The pattern for extracting the row from `addRow` calls appears in both `test_store_current_reading_*` tests. A small helper would reduce duplication and improve readability.

<details>
<summary>♻️ Suggested helper extraction</summary>

```python
def _extract_row_from_call(call: Any) -> dict[str, Any]:
    """Extract the row dict from an addRow mock call."""
    row = call.kwargs.get("row")
    if row is None:
        assert call.args, "Expected addRow to include row payload"
        row = call.args[0]
    return row
```

Then use as:

```python
row = _extract_row_from_call(writer.addRow.call_args)
\# or
rows = [_extract_row_from_call(c) for c in writer.addRow.call_args_list]
```

</details>

Also applies to: 268-273

---

### `meshtastic/tests/test_slog_power_logger.py:158`

**Type:** nitpick

**Module reload approach is fragile; consider guarding the cleanup reload.**

The test reloads `slog_module` with modified typing state. If the reload in the `finally` block (line 166) fails, subsequent tests may run against corrupted module state. Consider wrapping the cleanup reload in a nested try-except or using `pytest.importorskip` patterns.

<details>
<summary>🛡️ Suggested defensive cleanup</summary>

```diff
     finally:
         monkeypatch.setattr(typing, "TYPE_CHECKING", False)
         monkeypatch.setattr(pa, "DataType", original_data_type)
-        importlib.reload(slog_module)
+        try:
+            importlib.reload(slog_module)
+        except Exception:
+            pytest.fail("Failed to restore slog_module state after TYPE_CHECKING test")
```

</details>

---

### `meshtastic/tests/test_serial_interface.py:92`

**Type:** nitpick

**Consider adding pylint disable for consistency.**

This test has 5 mock parameters, which may trigger pylint's `R0917` (too-many-positional-arguments). The similar `test_SerialInterface_single_port` above uses `# pylint: disable=R0917` for its 6 parameters. Consider adding the same disable here for consistency, or apply the disable at module level to cover all test functions with many mock dependencies.

<details>
<summary>♻️ Optional: Add pylint disable</summary>

```diff
+# pylint: disable=R0917
 @pytest.mark.unit
 @patch("time.sleep")
 @patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
```

Or apply once at module level after imports:

```python
\# pylint: disable=R0917  # Tests with many mocked dependencies
```

</details>

---

### `meshtastic/tests/test_powermon_riden.py:170`

**Type:** nitpick

**Add a trailing newline at end of file.**

PEP 8 recommends files end with a single newline character. Line 172 appears to be missing the trailing newline.

---

### `meshtastic/tests/test_powermon_ppk2.py:8`

**Type:** nitpick

**Consider using absolute import for pytest compatibility.**

Relative imports in test files can cause issues with pytest discovery when running from different working directories. Absolute imports are generally more robust.

<details>
<summary>♻️ Suggested change</summary>

```diff
 try:
-    from ..powermon.ppk2 import PPK2PowerSupply
+    from meshtastic.powermon.ppk2 import PPK2PowerSupply
 except ImportError:
     pytest.skip("Can't import PPK2PowerSupply", allow_module_level=True)
```

</details>

---

### `meshtastic/tests/test_powermon_power_supply.py:93`

**Type:** nitpick

**Consider testing all snake_case shims for completeness.**

This test only covers `get_average_current_mA`. Based on learnings, other snake_case shims should also exist (`get_min_current_mA`, `get_max_current_mA`, `reset_measurements`). Consider parametrizing this test or adding separate tests to verify all snake_case aliases behave as stable shims without deprecation warnings.

<details>
<summary>♻️ Suggested parametrized approach</summary>

```diff
 @pytest.mark.unit
-def test_sim_power_supply_snake_case_alias_is_stable_shim(
+@pytest.mark.parametrize(
+    ("snake_alias", "canonical"),
+    [
+        ("get_average_current_mA", "getAverageCurrentMA"),
+        ("get_min_current_mA", "getMinCurrentMA"),
+        ("get_max_current_mA", "getMaxCurrentMA"),
+    ],
+)
+def test_sim_power_supply_snake_case_alias_is_stable_shim(
     monkeypatch: pytest.MonkeyPatch,
+    snake_alias: str,
+    canonical: str,
 ) -> None:
     """SimPowerSupply snake_case alias should delegate without emitting deprecation warnings."""
     monkeypatch.setattr("meshtastic.powermon.sim.time.time", lambda: 0.0)
     supply = SimPowerSupply()

     with warnings.catch_warnings(record=True) as caught:
         warnings.simplefilter("always")
-        value = supply.get_average_current_mA()
+        value = getattr(supply, snake_alias)()

-    assert value == pytest.approx(supply.getAverageCurrentMA())
+    assert value == pytest.approx(getattr(supply, canonical)())
     assert not [
         warning
         for warning in caught
         if issubclass(warning.category, DeprecationWarning)
     ]
```

</details>

Based on learnings: "Powermon snake_case shims should be provided (e.g., get_average_current_mA, get_min_current_mA, get_max_current_mA, reset_measurements)"

---

### `meshtastic/tests/test_ota.py:19`

**Type:** nitpick

**LGTM!** Type annotations are correctly added.

Consider using pytest's `tmp_path` fixture instead of manual `tempfile.NamedTemporaryFile` + `os.unlink` cleanup for cleaner test code. However, the current pattern is consistent throughout the file and functions correctly.

---

### `meshtastic/tests/test_node.py:78`

**Type:** nitpick

**Remove misleading unused variable assignment.**

`adminIndex` is actually used at line 85 (`captured["adminIndex"] = adminIndex`), so assigning it to `_` is misleading and suggests the parameter is unused when it isn't.

<details>
<summary>♻️ Proposed fix</summary>

```diff
     ) -> mesh_pb2.MeshPacket | None:
-        _ = adminIndex
         if sent_messages is not None:
             sent_messages.append(msg)
```

</details>

---

### `meshtastic/tests/test_mesh_interface.py:344`

**Type:** nitpick

**Consider removing redundant wait.**

The second `close_done.wait(timeout=0.05)` on Line 346 appears redundant after the 0.2s wait on Line 345 already confirmed that `close()` is blocked. If both assertions are intended to verify the blocking behavior, a single wait with a clear comment would suffice.

<details>
<summary>♻️ Suggested simplification</summary>

```diff
         # close() should block until the in-flight heartbeat send completes.
         # Use a generous timeout (0.2s) to avoid flakiness on slow CI runners.
         assert not close_done.wait(timeout=0.2)
-        assert not close_done.wait(timeout=0.05)
```

</details>

---

### `meshtastic/tests/test_main.py:2890`

**Type:** nitpick

**Consider extracting shared setup into a fixture for long round-trip test.**

This test is comprehensive but quite long (~150 lines). The setup for `source_local` and `source_module` configuration could be extracted into a reusable fixture, which would make the test more focused on the round-trip verification logic and improve maintainability.

---

### `meshtastic/tests/test_main.py:2799`

**Type:** nitpick

**Incomplete docstring for helper function.**

The `_build_configure_interface` helper is missing the `Returns` section in its NumPy-style docstring, which would help clarify what the tuple elements represent.

<details>
<summary>📝 Suggested docstring addition</summary>

```diff
 def _build_configure_interface(
     target_local: localonly_pb2.LocalConfig | None = None,
     target_module: localonly_pb2.LocalModuleConfig | None = None,
 ) -> tuple[MagicMock, MagicMock]:
-    """Build a minimal interface mock compatible with --configure operations."""
+    """Build a minimal interface mock compatible with --configure operations.
+
+    Parameters
+    ----------
+    target_local : localonly_pb2.LocalConfig | None
+        Local config protobuf to use, or None to create a new one.
+    target_module : localonly_pb2.LocalModuleConfig | None
+        Module config protobuf to use, or None to create a new one.
+
+    Returns
+    -------
+    tuple[MagicMock, MagicMock]
+        A tuple of (interface_mock, target_node_mock).
+    """
     if target_local is None:
```

</details>

---

### `meshtastic/tests/test_int.py:10`

**Type:** nitpick

**Consider defensive handling for potential None values.**

If `run_cli_argv_with_timeout` could return `None` for `stdout` or `stderr` (e.g., when not captured), the concatenation would fail. Adding a fallback ensures robustness.

<details>
<summary>🛡️ Suggested defensive handling</summary>

```diff
 def _run_and_collect(cmd: list[str]) -> tuple[int, str]:
     """Run CLI argv command and return (returncode, combined output)."""
     result = run_cli_argv_with_timeout(cmd)
-    return result.returncode, result.stdout + result.stderr
+    return result.returncode, (result.stdout or "") + (result.stderr or "")
```

</details>

---

### `meshtastic/tests/test_examples.py:42`

**Type:** nitpick

**Consider clarifying expected exit behavior.**

The test correctly mocks `serial.tools.list_ports.comports` to simulate a no-device environment and verifies the warning is logged. However, unlike the no-argument test, this doesn't wrap the call in `pytest.raises(SystemExit)`.

If the script is expected to exit gracefully (no `SystemExit`) when no device is found, consider adding an explicit comment or assertion to document this expected behavior. Alternatively, if the script does exit, wrapping in `pytest.raises` would make the test more robust.

```diff
+    # Script logs warning but continues/returns normally when no device found
     with caplog.at_level(logging.WARNING):
         _run_hello_world_serial(monkeypatch, "hello")
```

---

### `meshtastic/tests/conftest.py:373`

**Type:** nitpick

**Note the attribute initialization complexity in `ppk2_stub`.**

The stub sets both `measurement_thread` (line 382) and `_measurement_thread` (line 390) to different values. If the actual `PPK2PowerSupply` class uses only one of these attributes, the other assignment may be unnecessary. If both exist, consider adding a brief comment clarifying their relationship.

```diff
     ppk.measurement_thread = threading.Thread(
         target=lambda: None, daemon=True, name="ppk2 test thread"
     )
     ppk._v = 3.3
     ppk_any = cast(Any, ppk)
     ppk_any._is_supply = False
     ppk_any._closed = False
     ppk_any._shutdown_event = threading.Event()
-    ppk_any._measurement_thread = None
+    ppk_any._measurement_thread = None  # Internal reference; measurement_thread is the public handle
```

---

### `meshtastic/tests/conftest.py:143`

**Type:** nitpick

**Complex but necessary test isolation fixture.**

The logic for snapshotting/restoring `mt_config` state and warn-once registries is thorough. Consider extracting the repeated lock-guarded set operations into a helper to reduce duplication:

<details>
<summary>♻️ Optional: Extract lock-guarded set operations</summary>

```python
def _update_set_with_lock(
    target: set[str],
    source: set[str],
    lock: threading.Lock | None,
) -> None:
    """Clear target set and update from source, optionally under lock."""
    if lock is not None:
        with lock:
            target.clear()
            target.update(source)
    else:
        target.clear()
        target.update(source)
```

This could simplify the restore logic at lines 195-206.

</details>

---

### `meshtastic/tests/cli_test_utils.py:77`

**Type:** nitpick

**Same timeout type consideration as above.**

For consistency with `subprocess.run` and the internal helper, consider `int | float` for the timeout parameter here as well.

<details>
<summary>Suggested type hint adjustment</summary>

```diff
 def run_cli_argv_with_timeout(
-    cmd: list[str], timeout: int = 30
+    cmd: list[str], timeout: int | float = 30
 ) -> subprocess.CompletedProcess[str]:
```

</details>

---

### `meshtastic/tests/cli_test_utils.py:32`

**Type:** nitpick

**Consider widening timeout type to `int | float` for consistency.**

The internal helper `_fail_masked_timeout` accepts `int | float`, and `subprocess.run`'s timeout parameter accepts `float | None`. Accepting `float` here would provide more flexibility for callers.

<details>
<summary>Suggested type hint adjustment</summary>

```diff
-def run_cli_with_timeout(command: str, timeout: int = 120) -> tuple[int, str]:
+def run_cli_with_timeout(command: str, timeout: int | float = 120) -> tuple[int, str]:
```

</details>

---

### `meshtastic/tcp_interface.py:581`

**Type:** nitpick

**Consider resetting counters only after sustained successful communication.**

Resetting `_reconnect_attempts` and `_fatal_disconnect` on every successful read means a single byte received will reset the counter. If the connection is flaky, this could allow more reconnect cycles than intended. Consider requiring multiple successful reads or a time-based stability check before resetting. This is a minor robustness consideration.

---

### `meshtastic/tcp_interface.py:350`

**Type:** nitpick

**Minor race window in socket access.**

The socket is read without holding `_reconnect_lock` (line 350), then used for `sendall()` (line 355). A concurrent close could set `self.socket = None` or close the socket between these operations. The `OSError` catch handles this gracefully, but the error message might be confusing. This is acceptable given the exception handling, but documenting the intentional unlocked read would clarify the design.

---

### `meshtastic/tcp_interface.py:291`

**Type:** nitpick

**Event.set() may not convey failure semantics to waiters.**

When `signal_set` is an `Event.set()`, calling it signals completion but doesn't communicate the failure reason. Waiters using this pattern would need to check additional state to know the operation failed. This is a minor semantic concern—consider documenting this limitation.

---

### `meshtastic/tcp_interface.py:192`

**Type:** nitpick

**Polling loop with sleep could be improved with condition variable.**

The `while True` loop with `time.sleep(DEFAULT_RECONNECT_SLEEP_SLICE)` is a busy-wait pattern. Consider using a `threading.Condition` to wake waiters when the reconnect completes, which would be more efficient than periodic polling. This is a minor efficiency concern given the 0.25s sleep interval.

---

### `meshtastic/supported_device.py:40`

**Type:** nitpick

**Consider updating docstring to reflect full scope.**

The docstring says "Normalize USB ID fields" but the method also performs validation. A more accurate description might be "Validate and normalize USB ID fields to canonical lowercase form."

<details>
<summary>📝 Suggested docstring</summary>

```diff
     def __post_init__(self) -> None:
-        """Normalize USB ID fields to canonical lowercase tuple form."""
+        """Validate and normalize USB ID fields to canonical lowercase form."""
```

</details>

---

### `meshtastic/stream_interface.py:459`

**Type:** nitpick

**Single-byte reads may be inefficient for throughput.**

Reading one byte at a time via `_read_bytes(1)` is simple but could be a performance bottleneck when handling large or frequent messages. Consider buffering larger reads (e.g., read available bytes up to a limit) and processing from a buffer if throughput becomes a concern.

---

### `meshtastic/stream_interface.py:274`

**Type:** nitpick

**Write loop lacks timeout protection.**

The partial-write loop will retry indefinitely if `write()` consistently returns 0 or small values. While `written_count <= 0` raises immediately, a misbehaving stream returning very small positive values could cause extended blocking. Consider adding a maximum iteration count or total timeout if this is a concern for your use cases.

---

### `meshtastic/stream_interface.py:156`

**Type:** nitpick

**Redundant stream validation after initial check.**

The check at lines 160-167 duplicates the earlier validation at line 126. By this point, if `connectNow` is true and we reach line 160, we've already validated that either a stream exists, `noProto` is true, or `_provides_own_stream` is set. The inner `if noProto` branch at line 161 can never raise because `noProto=True` was already handled at line 126.

Consider simplifying:

<details>
<summary>♻️ Simplified logic</summary>

```diff
         if connectNow:
-            # Use a sentinel attribute to detect if subclass provides its own stream I/O.
-            # This is more robust than method identity checks which break with decorators.
-            if self.stream is None and not _provides_own_stream:
-                if noProto:
-                    logger.debug(
-                        "No stream configured for %s; deferring connect()",
-                        self.__class__.__name__,
-                    )
-                else:
-                    raise StreamInterface.StreamInterfaceError()
-            else:
-                self.connect()
-                if not noProto:
-                    # connect() waits only for transport-connected state; constructor
-                    # still waits for full config materialization for legacy behavior.
-                    self.waitForConfig()
+            self.connect()
+            if not noProto:
+                # connect() waits only for transport-connected state; constructor
+                # still waits for full config materialization for legacy behavior.
+                self.waitForConfig()
```

</details>

---

### `meshtastic/stream_interface.py:28`

**Type:** nitpick

**Consider consolidating identical exception tuples.**

`STREAM_WRITE_EXCEPTIONS` and `STREAM_READ_EXCEPTIONS` are currently identical. If they're expected to diverge in the future, the duplication is fine; otherwise, a single `STREAM_IO_EXCEPTIONS` tuple would reduce maintenance burden.

---

### `meshtastic/serial_interface.py:190`

**Type:** nitpick

**Minor inconsistency in attribute access pattern.**

`noProto` is accessed directly (line 193) while `debugOut` and `noNodes` use `hasattr` checks (lines 191, 195). If defensive checks are needed for partially initialized instances, apply them consistently; otherwise, remove them for attributes guaranteed by the parent class.

---

### `meshtastic/serial_interface.py:38`

**Type:** nitpick

**Consider expanding docstring to NumPy-style format.**

The method has a brief docstring but lacks the parameter and return documentation in NumPy style. Since this is an internal method, this is a minor suggestion.

<details>
<summary>📝 Suggested docstring expansion</summary>

```diff
     def _resolve_dev_path(self) -> str | None:
-        """Return an explicit or auto-detected serial device path."""
+        """Return an explicit or auto-detected serial device path.
+
+        Returns
+        -------
+        str | None
+            The resolved device path, or None if no devices are found.
+
+        Raises
+        ------
+        MeshInterfaceError
+            If devPath is an empty string, or if multiple ports are detected.
+        """
         if self.devPath is not None:
```

</details>

---

### `meshtastic/remote_hardware.py:460`

**Type:** nitpick

**LGTM!**

Good design to store the watch mask for correlation with incoming responses. The lock acquisition around the dict mutation is correct.

Minor nit: `int(mask)` on line 484 is redundant since `_validate_non_negative_int` already returns an `int`.

<details>
<summary>♻️ Remove redundant int() cast</summary>

```diff
         if node_key is not None:
             with _get_watch_masks_lock(self.iface):
-                _get_watch_masks(self.iface)[node_key] = int(mask)
+                _get_watch_masks(self.iface)[node_key] = mask
```

</details>

---

### `meshtastic/remote_hardware.py:112`

**Type:** nitpick

**Minor: Fallback logic may be redundant.**

The fallback on lines 120-123 checks `isdigit()` after `int(normalized, 0)` fails. Since `int(..., 0)` handles all standard decimal/hex/octal/binary formats including signed numbers, this fallback appears to rarely (if ever) trigger. It's harmless but could be simplified.

<details>
<summary>♻️ Optional simplification</summary>

```diff
 def _parse_node_number(text: str) -> int | None:
     """Parse node number text from prefixed base forms or plain decimal."""
     normalized = text.strip().lower()
     if not normalized:
         return None
     try:
         return int(normalized, 0)
     except ValueError:
-        signless = normalized[1:] if normalized[:1] in "+-" else normalized
-        if signless.isdigit() and signless:
-            return int(normalized, 10)
         return None
```

</details>

---

### `meshtastic/powermon/stress.py:55`

**Type:** nitpick

**Consider expanding the docstring for `sendPowerStress`.**

The method has a minimal one-line docstring. For consistency with `syncPowerStress` and the coding guidelines requiring NumPy-style docstrings, consider adding parameter descriptions and return value documentation.

<details>
<summary>📝 Suggested docstring expansion</summary>

```diff
     def sendPowerStress(
         self,
         cmd: powermon_pb2.PowerStressMessage.Opcode.ValueType,
         num_seconds: float = 0.0,
         onResponse: Callable[[dict[str, Any]], None] | None = None,
     ) -> Any:
-        """Client goo for talking with the device side agent."""
+        """Send a power stress command to the device.
+
+        Parameters
+        ----------
+        cmd : powermon_pb2.PowerStressMessage.Opcode.ValueType
+            The power stress command opcode to send.
+        num_seconds : float
+            Duration for timed stress commands. (Default value = 0.0)
+        onResponse : Callable[[dict[str, Any]], None] | None
+            Optional callback invoked when a response is received.
+            (Default value = None)
+
+        Returns
+        -------
+        Any
+            The result from sendData (packet ID or send result).
+        """
         r = powermon_pb2.PowerStressMessage()
```

</details>

---

### `meshtastic/powermon/stress.py:23`

**Type:** nitpick

**Consider adding a snake_case shim for `handlePowerStressResponse`.**

Per learnings, powermon functions should provide snake_case shims (e.g., `handle_power_stress_response`). The current implementation provides `onPowerStressResponse` as a camelCase compatibility alias, but the snake_case convention for internal/helper use may be expected.

Additionally, the `interface` parameter is typed as `Any`. If `MeshInterface` is the expected type, consider using a more specific type hint or a forward reference.

<details>
<summary>♻️ Suggested snake_case shim</summary>

```diff
 \# COMPAT_STABLE_SHIM: naming alias for existing callback users.
 def onPowerStressResponse(packet: dict[str, Any], interface: Any) -> None:
     """Compatibility alias for handlePowerStressResponse()."""
     handlePowerStressResponse(packet, interface)
+
+
+# COMPAT_STABLE_SHIM: snake_case alias for internal/style consistency.
+def handle_power_stress_response(packet: dict[str, Any], interface: Any) -> None:
+    """Snake_case alias for handlePowerStressResponse()."""
+    handlePowerStressResponse(packet, interface)
```

</details>

Based on learnings: "Powermon snake_case shims should be provided."

---

### `meshtastic/powermon/riden.py:65`

**Type:** nitpick

**Missing snake_case compatibility shim for `get_average_current_mA`.**

According to the project's compatibility guidelines, a snake_case shim `get_average_current_mA` should be provided alongside the promoted camelCase method `getAverageCurrentMA`. This mirrors the pattern used for `_getRawWattHour` → `_get_raw_watt_hour`.

<details>
<summary>♻️ Proposed fix to add compatibility shim</summary>

```diff
     def getAverageCurrentMA(self) -> float:
         """Return average current of last measurement in mA since last call to this method."""
         now = time.monotonic()
         nowWattHour = self._get_raw_watt_hour()
         self.nowWattHour = nowWattHour
         elapsed_s = now - self.prevPowerTime
         if elapsed_s <= 0:
             # Consume the window to avoid stale deltas on subsequent reads.
             self.prevPowerTime = now
             self.prevWattHour = nowWattHour
             return math.nan
         delta_watt_hour = nowWattHour - self.prevWattHour
         # Intentional: consume this measurement window even when voltage <= 0 to avoid a
         # large energy spike after voltage recovers.
         self.prevPowerTime = now
         self.prevWattHour = nowWattHour
         if delta_watt_hour < 0:
             # Counter reset/rollover or transient read glitch; resync baseline.
             return math.nan
         watts = (delta_watt_hour / elapsed_s) * SECONDS_PER_HOUR
         if self.v <= 0:
             return math.nan
         return (watts / self.v) * MILLIAMPS_PER_AMP
+
+    # COMPAT_STABLE_SHIM: snake_case alias for external integrations.
+    def get_average_current_mA(self) -> float:  # pylint: disable=invalid-name
+        """Compatibility alias for getAverageCurrentMA()."""
+        return self.getAverageCurrentMA()
```

</details>

Based on learnings: "PowerMeter snake_case compatibility shims should be provided: get_average_current_mA, get_min_current_mA, get_max_current_mA, reset_measurements"

---

### `meshtastic/powermon/ppk2.py:209`

**Type:** nitpick

**Consider adding `reset_measurements` shim.**

Same pattern as the getter methods—a snake_case alias should be provided for consistency with other PowerMeter interfaces.

<details>
<summary>♻️ Proposed shim</summary>

```python
    # COMPAT_STABLE_SHIM - snake_case alias
    def reset_measurements(self) -> None:
        """Alias for resetMeasurements."""
        self.resetMeasurements()
```

</details>

Based on learnings: "PowerMeter snake_case compatibility shims should be provided: [...] reset_measurements"

---

### `meshtastic/powermon/ppk2.py:163`

**Type:** nitpick

**Missing snake_case compatibility shims per project conventions.**

The canonical camelCase methods (`getMinCurrentMA`, `getMaxCurrentMA`, `getAverageCurrentMA`) are correctly implemented. However, per project learnings, PowerMeter classes should also provide snake_case compatibility shims: `get_min_current_mA`, `get_max_current_mA`, `get_average_current_mA`.

<details>
<summary>♻️ Proposed snake_case shims (silent compatibility wrappers)</summary>

```python
    # COMPAT_STABLE_SHIM - snake_case aliases for naming consistency
    def get_min_current_mA(self) -> float:
        """Alias for getMinCurrentMA."""
        return self.getMinCurrentMA()

    def get_max_current_mA(self) -> float:
        """Alias for getMaxCurrentMA."""
        return self.getMaxCurrentMA()

    def get_average_current_mA(self) -> float:
        """Alias for getAverageCurrentMA."""
        return self.getAverageCurrentMA()
```

</details>

Based on learnings: "PowerMeter snake_case compatibility shims should be provided: get_average_current_mA, get_min_current_mA, get_max_current_mA, reset_measurements"

---

### `meshtastic/ota.py:86`

**Type:** nitpick

**Consider extracting timeout as a named constant.**

The socket timeout value could be a module-level constant for clarity and easier configuration.

<details>
<summary>♻️ Suggested refactor</summary>

At module level:

```python
_OTA_SOCKET_TIMEOUT_SECONDS = 15
_OTA_CHUNK_SIZE = 1024
```

Then use:

```diff
-        self._socket.settimeout(15)
+        self._socket.settimeout(_OTA_SOCKET_TIMEOUT_SECONDS)
```

```diff
-            chunk_size = 1024
+            chunk_size = _OTA_CHUNK_SIZE
```

</details>

As per coding guidelines: "Prefer named module-level constants (UPPER_SNAKE_CASE) for repeated literals (magic numbers/strings, timeouts...)".

---

### `meshtastic/node.py:2210`

**Type:** nitpick

**Inconsistent shim delegation pattern.**

The comment says "alias for getChannelsWithHash" but `get_channels_with_hash()` calls `_get_channels_with_hash()` directly, bypassing `getChannelsWithHash()`. This differs from other shims like `get_ringtone()` → `getRingtone()` → `_get_ringtone()`.

<details>
<summary>💡 Suggested fix for consistency</summary>

```diff
     # COMPAT_STABLE_SHIM: alias for getChannelsWithHash
     def get_channels_with_hash(self) -> list[dict[str, Any]]:
         """Get channel entries with computed per-channel hashes.
         ...
         """
-        return self._get_channels_with_hash()
+        return self.getChannelsWithHash()
```

This maintains the delegation chain: `get_channels_with_hash()` → `getChannelsWithHash()` → `_get_channels_with_hash()`, consistent with other method patterns.

</details>

---

### `meshtastic/node.py:1202`

**Type:** nitpick

**Extract canned message max length as a module constant.**

Similar to the ringtone limit, the magic number `200` should be a named constant.

<details>
<summary>💡 Suggested constant extraction</summary>

Add at module level:

```python
MAX_CANNED_MESSAGE_LEN = 200
```

Then update:

```diff
-        if len(message) > 200:
-            self._raise_interface_error(
-                "The canned message must be 200 characters or fewer."
-            )
+        if len(message) > MAX_CANNED_MESSAGE_LEN:
+            self._raise_interface_error(
+                f"The canned message must be {MAX_CANNED_MESSAGE_LEN} characters or fewer."
+            )
```

</details>

---

### `meshtastic/node.py:1044`

**Type:** nitpick

**Extract ringtone max length as a module constant.**

The magic number `230` for ringtone max length should be a named constant for clarity and maintainability.

<details>
<summary>💡 Suggested constant extraction</summary>

Add at module level:

```python
MAX_RINGTONE_LEN = 230
```

Then update the validation:

```diff
-        if len(ringtone) > 230:
-            self._raise_interface_error("The ringtone must be 230 characters or fewer.")
+        if len(ringtone) > MAX_RINGTONE_LEN:
+            self._raise_interface_error(
+                f"The ringtone must be {MAX_RINGTONE_LEN} characters or fewer."
+            )
```

</details>

---

### `meshtastic/node.py:720`

**Type:** nitpick

**Consider extracting short name max length as a module constant.**

`nChars = 4` could be a module-level constant `MAX_SHORT_NAME_LEN = 4` for consistency with `MAX_LONG_NAME_LEN`. This follows the guideline to prefer named constants for repeated literals.

<details>
<summary>💡 Suggested constant extraction</summary>

Add at module level (near line 56):

```python
MAX_SHORT_NAME_LEN = 4
```

Then update the method:

```diff
-        nChars = 4
         if short_name is not None:
             short_name = short_name.strip()
             # Validate that short_name is not empty or whitespace-only
             if not short_name:
                 self._raise_interface_error(EMPTY_SHORT_NAME_MSG)
-            if len(short_name) > nChars:
-                short_name = short_name[:nChars]
+            if len(short_name) > MAX_SHORT_NAME_LEN:
+                short_name = short_name[:MAX_SHORT_NAME_LEN]
                 logger.warning(
-                    f"Short name is longer than {nChars} characters, truncating to '{short_name}'"
+                    f"Short name is longer than {MAX_SHORT_NAME_LEN} characters, truncating to '{short_name}'"
                 )
```

</details>

---

### `meshtastic/node.py:348`

**Type:** nitpick

**Type hint `int | Any` is effectively just `Any`.**

The union `int | Any` doesn't add value since `Any` already includes all types. Consider using a more specific type that reflects the actual expected types.

<details>
<summary>💡 Suggested type hint improvement</summary>

```diff
-    def requestConfig(self, configType: int | Any) -> None:
+    def requestConfig(
+        self, configType: int | "google.protobuf.descriptor.FieldDescriptor"
+    ) -> None:
```

You may need to import the appropriate type or define a protocol/type alias for the protobuf field descriptor.

</details>

---

### `meshtastic/mt_config.py:61`

**Type:** nitpick

**Consider consolidating the duplicated clearing logic.**

The logic for clearing `_warned_deprecations` is repeated in both branches. Since the lock-less path only runs during module import (single-threaded bootstrap), this is safe but could be simplified.

<details>
<summary>♻️ Optional consolidation</summary>

```diff
     # reset() runs once before compatibility warn-once state is defined below,
     # so this bootstrap path must tolerate lock/set absence during import.
     warned_deprecations_lock = module_globals.get("_warned_deprecations_lock")
-    if warned_deprecations_lock is not None:
-        with warned_deprecations_lock:
-            warned_deprecations = module_globals.get("_warned_deprecations")
-            if isinstance(warned_deprecations, set):
-                warned_deprecations.clear()
-    else:
-        warned_deprecations = module_globals.get("_warned_deprecations")
-        if isinstance(warned_deprecations, set):
-            warned_deprecations.clear()
+    warned_deprecations = module_globals.get("_warned_deprecations")
+    if isinstance(warned_deprecations, set):
+        if warned_deprecations_lock is not None:
+            with warned_deprecations_lock:
+                warned_deprecations.clear()
+        else:
+            warned_deprecations.clear()
```

</details>

---

### `meshtastic/interfaces/ble/utils.py:94`

**Type:** nitpick

**Public function should use camelCase per project conventions.**

This public utility function should also follow the camelCase convention.

<details>
<summary>♻️ Proposed rename</summary>

```diff
-def resolve_ble_module() -> ModuleType | None:
+def resolveBleModule() -> ModuleType | None:
     """Locate and return the first available BLE-related module for the package.
```

</details>

As per coding guidelines: "Functions/methods: use `camelCase` for public API".

---

### `meshtastic/interfaces/ble/utils.py:52`

**Type:** nitpick

**Public function should use camelCase per project conventions.**

Similar to `sanitize_address`, this public utility function should follow the camelCase convention.

<details>
<summary>♻️ Proposed rename</summary>

```diff
-async def with_timeout(
+async def withTimeout(
     awaitable: Awaitable[T],
     timeout: float | None,
     label: str,
     timeout_error_factory: Callable[[str, float], Exception] | None = None,
 ) -> T:
-    """Run an awaitable with an optional timeout.
+    """Run an awaitable with an optional timeout.
```

</details>

As per coding guidelines: "Functions/methods: use `camelCase` for public API".

---

### `meshtastic/interfaces/ble/utils.py:12`

**Type:** nitpick

**Public function should use camelCase per project conventions.**

This is a new utility function, not a historical BLE method covered by the compatibility exception. Per coding guidelines, public API functions should use camelCase.

<details>
<summary>♻️ Proposed rename</summary>

```diff
-def sanitize_address(address: str | None) -> str | None:
-    """Normalize a BLE address or identifier by removing common separators and converting to lowercase.
+def sanitizeAddress(address: str | None) -> str | None:
+    """Normalize a BLE address or identifier by removing common separators and converting to lowercase.
```

</details>

As per coding guidelines: "Standard public methods should use camelCase (e.g., sendText, sendData)" and "Functions/methods: use `camelCase` for public API".

---

### `meshtastic/interfaces/ble/runner.py:451`

**Type:** nitpick

**Consider using warn-once behavior for the deprecation warning.**

The current implementation emits a `DeprecationWarning` on every call when the deprecated `timeout` parameter is used. If this method is called in a loop (e.g., submitting multiple coroutines), this could result in warning spam.

Consider using Python's built-in warn-once filtering or a module-level flag to ensure the warning is only emitted once:

<details>
<summary>♻️ Proposed warn-once pattern</summary>

```diff
+_timeout_deprecation_warned = False
+
 def _run_coroutine_threadsafe(
     self,
     coro: Coroutine[Any, Any, T],
     timeout: float | None = None,
     *,
     startup_timeout: float | None = None,
 ) -> Future[T]:
     ...
     if timeout is not None and startup_timeout is not None:
         raise ValueError(BLECLIENT_ERROR_TIMEOUT_PARAM_CONFLICT)
     if timeout is not None and startup_timeout is None:
+        global _timeout_deprecation_warned
+        if not _timeout_deprecation_warned:
+            _timeout_deprecation_warned = True
             warnings.warn(
                 "run_coroutine_threadsafe(timeout=...) is deprecated; "
                 "use startup_timeout=<seconds> instead.",
                 DeprecationWarning,
                 stacklevel=2,
             )
```

</details>

As per coding guidelines: "For deprecated APIs that may be called in loops, use warn-once behavior to avoid warning spam."

---

### `meshtastic/interfaces/ble/policies.py:270`

**Type:** nitpick

**Consider extracting repeated policy values to named constants.**

The values `backoff=1.5` and `jitter_ratio=0.1` are repeated in both `_empty_read` and `_transient_error` factory methods. For consistency with how `AUTO_RECONNECT` uses `BLEConfig` constants, consider adding these to `BLEConfig` or as module-level constants.

<details>
<summary>♻️ Suggested constant extraction</summary>

Add to `BLEConfig` (or as module-level constants):

```python
\# In constants.py or at module level
DEFAULT_RETRY_BACKOFF = 1.5
DEFAULT_RETRY_JITTER_RATIO = 0.1
```

Then use in the factory methods:

```diff
     @staticmethod
     def _empty_read() -> ReconnectPolicy:
         return ReconnectPolicy(
             initial_delay=BLEConfig.EMPTY_READ_RETRY_DELAY,
             max_delay=1.0,
-            backoff=1.5,
-            jitter_ratio=0.1,
+            backoff=BLEConfig.DEFAULT_RETRY_BACKOFF,
+            jitter_ratio=BLEConfig.DEFAULT_RETRY_JITTER_RATIO,
             max_retries=BLEConfig.EMPTY_READ_MAX_RETRIES,
         )

     @staticmethod
     def _transient_error() -> ReconnectPolicy:
         return ReconnectPolicy(
             initial_delay=BLEConfig.TRANSIENT_READ_RETRY_DELAY,
             max_delay=2.0,
-            backoff=1.5,
-            jitter_ratio=0.1,
+            backoff=BLEConfig.DEFAULT_RETRY_BACKOFF,
+            jitter_ratio=BLEConfig.DEFAULT_RETRY_JITTER_RATIO,
             max_retries=BLEConfig.TRANSIENT_READ_MAX_RETRIES,
         )
```

</details>

As per coding guidelines: "Prefer named module-level constants (UPPER_SNAKE_CASE) for repeated literals (magic numbers/strings, timeouts, retry/backoff values)".

---

### `meshtastic/interfaces/ble/interface.py:1570`

**Type:** nitpick

**Redundant null check for local variable.**

The check at lines 1574-1575 is redundant since `connected_client` is a local variable that was already validated at lines 1570-1571. The value cannot change between these two checks.

<details>
<summary>🔧 Suggested simplification</summary>

```diff
                 if connected_client is None:
                     raise self.BLEError(ERROR_NO_CLIENT_ESTABLISHED)
         # Finalize after the per-address lock scope exits to avoid nested
         # lock-order inversions when gate finalization reacquires address locks.
-        if connected_client is None:
-            raise self.BLEError(ERROR_NO_CLIENT_ESTABLISHED)
         self._finalize_connection_gates(
             connected_client, connected_device_key, connection_alias_key
         )
```

</details>

---

### `meshtastic/interfaces/ble/gating.py:313`

**Type:** nitpick

**CPython-specific `_is_owned` method may not exist on other Python implementations.**

The `_is_owned()` method is an internal CPython implementation detail of `RLock` and is not part of the public threading API. This defensive check will silently pass on PyPy, Jython, or other implementations where the method doesn't exist.

If portability matters, consider documenting this as a CPython-only assertion or removing the check since the function's precondition is already documented in the docstring.

```web
Python RLock _is_owned method availability
```

---

### `meshtastic/interfaces/ble/errors.py:26`

**Type:** nitpick

**Minor observation: Internal class exported in `__all__`.**

`BLEErrorHandler` is documented as an "Internal helper class" (line 33) but is exported via `__all__`. This is acceptable if other internal BLE modules need to import it, but consider whether it should remain in `__all__` or if importers should use explicit imports instead to keep the public surface narrow.

Based on learnings: "Do not leak internal BLE modules/symbols" and "Keep BLE public exports explicit and narrow."

---

### `meshtastic/interfaces/ble/discovery.py:543`

**Type:** nitpick

**Naming: `close` deviates from camelCase guideline.**

Per coding guidelines, standard public methods should use camelCase (e.g., `sendText`, `sendData`). However, `close` follows Python's standard closeable protocol (used by `with` statements, file-like objects, etc.), which is a reasonable exception for consistency with the Python ecosystem.

If intentional, consider adding a brief comment noting this is for protocol compatibility.

---

### `meshtastic/interfaces/ble/discovery.py:487`

**Type:** nitpick

**Minor: clarify control flow around potential None cast.**

When `invalid_client_error` is set (lines 472-485), `self._client` is `None`, so the cast on line 487 produces a `None` value typed as `BLEClient | DiscoveryClientProtocol`. The error is raised on line 498 before `client` is used, so this is safe at runtime—but the cast is misleading.

Consider moving line 487 inside an `else` branch or adding a guard comment for clarity.

<details>
<summary>💡 Optional: clarify intent</summary>

```diff
         if invalid_client_error is not None:
             raise invalid_client_error
+
+        # client is guaranteed non-None here after validation above
         client = cast(BLEClient | DiscoveryClientProtocol, self._client)
-
-    seen_stale_ids: set[int] = set()
```

Or move the cast after the raise check outside the lock scope.

</details>

---

### `meshtastic/interfaces/ble/discovery.py:146`

**Type:** nitpick

**Questionable fallback: closing an already-consumed awaitable.**

At this point, `awaitable_result` has been passed to `asyncio.wait_for()` inside `create_task()`. If `create_task()` fails, the coroutine wrapped by `wait_for()` may already be scheduled or in an undefined state. Calling `.close()` on `awaitable_result` (the original awaitable) likely has no effect since `wait_for()` created a new coroutine wrapper.

Consider whether this fallback is meaningful or if it can be removed to simplify the error path.

---

### `meshtastic/interfaces/ble/coordination.py:292`

**Type:** nitpick

**Consider adding debug assertions for lock ownership.**

The `_no_lock` methods document that callers must hold `self._lock`, but there's no runtime verification. Consider adding debug assertions to catch misuse during development.

<details>
<summary>♻️ Optional: Add debug assertions</summary>

```diff
 def _set_event_no_lock(self, name: str) -> None:
     """Set the named tracked event without acquiring the coordinator lock.

     Caller must hold self._lock.

     If no event with the given name is registered, this is a no-op.

     Parameters
     ----------
     name : str
         Name of the event to retrieve.
     """
+    assert self._lock._is_owned(), "Caller must hold self._lock"
     event = self._events.get(name)
     if event is not None:
         event.set()

 def _clear_event_no_lock(self, name: str) -> None:
     """Clear the tracked event named `name` without acquiring the coordinator lock.

     Caller must hold self._lock.

     If no event is registered under `name`, this is a no-op. This method clears the event so waiting threads will block until it is set again.

     Parameters
     ----------
     name : str
         The name of the event to clear.
     """
+    assert self._lock._is_owned(), "Caller must hold self._lock"
     event = self._events.get(name)
     if event is not None:
         event.clear()
```

</details>

---

### `meshtastic/interfaces/ble/connection.py:254`

**Type:** nitpick

**Consider simplifying `sys.is_finalizing` check.**

Since Python 3.10+ is the baseline, `sys.is_finalizing` is guaranteed to exist. The fallback `getattr` pattern is defensive but unnecessary.

<details>
<summary>♻️ Suggested simplification</summary>

```diff
-        skip_disconnect = bool(getattr(sys, "is_finalizing", lambda: False)())
+        skip_disconnect = sys.is_finalizing()
```

</details>

---

### `meshtastic/ble_interface.py:58`

**Type:** nitpick

**Minor: Loop variable leaks into module namespace.**

The loop variable `_symbol` remains in the module's namespace after the loop completes. While the underscore prefix signals it's internal, you could clean it up for completeness.

<details>
<summary>♻️ Optional cleanup</summary>

```diff
 _BLE_PUBLIC_ALL = tuple(getattr(_ble, "__all__", ()))
 for _symbol in _BLE_PUBLIC_ALL:
     globals().setdefault(_symbol, getattr(_ble, _symbol))
+del _symbol  # Clean up loop variable
```

Note: You'd need to guard with `if _BLE_PUBLIC_ALL:` to avoid `NameError` when the tuple is empty.

</details>

---

### `meshtastic/__main__.py:2139`

**Type:** nitpick

**Prefer lazy `%s` formatting for logger calls.**

Using f-strings in logging calls evaluates the string even when the log level would suppress the message. Use lazy formatting for better performance.

<details>
<summary>🔧 Suggested fix</summary>

```diff
-                    logger.info(f"Logging serial output to {args.seriallog}")
+                    logger.info("Logging serial output to %s", args.seriallog)
```

</details>

---

### `meshtastic/__main__.py:1616`

**Type:** nitpick

**Consider adding defensive None checks for robustness.**

The `fields_by_name.get()` call could theoretically return `None`, and `message_type` could be `None` for non-message fields. While this likely works in practice with Meshtastic protobufs (where config sections are always message types), adding defensive checks would make this more robust.

<details>
<summary>🛡️ Proposed defensive fix</summary>

```diff
 for config_section in objDesc.fields:
     if config_section.name != "version":
         section_field = objDesc.fields_by_name.get(config_section.name)
+        if section_field is None or section_field.message_type is None:
+            continue
         print(f"{config_section.name}:")
         names = []
         for field in section_field.message_type.fields:
```

</details>

---

### `examples/scan_for_devices.py:15`

**Type:** nitpick

**Good structure with type hint and proper error handling.**

The function signature with `-> None` type hint, stderr output for errors, and `SystemExit(3)` for argument violations is clean.

One optional consideration: the coding guidelines specify NumPy-style docstrings. For such a simple function, the one-liner is acceptable, but if you want strict adherence:

<details>
<summary>📝 Optional: NumPy-style docstring</summary>

```diff
 def main() -> None:
-    """Print detected supported Meshtastic devices and active ports."""
+    """Print detected supported Meshtastic devices and active ports.
+
+    Scans for hardware devices with known vendor IDs, displays detected
+    devices sorted by name/version/firmware, and lists active serial ports.
+    """
     if len(sys.argv) != 1:
```

</details>

As per coding guidelines: "Use NumPy-style docstrings for new and edited docstrings (Ruff pydocstyle convention)".

---

### `examples/pub_sub_example.py:14`

**Type:** nitpick

**Consider `_CONNECTED` for module-level constant reference.**

Per coding guidelines, constants should use `UPPER_SNAKE_CASE`. While the `Event` object is mutable, the module-level reference itself is constant. This is a minor nit for an example file.

<details>
<summary>♻️ Optional naming change</summary>

```diff
-_connected = threading.Event()
+_CONNECTED = threading.Event()
```

Then update references at lines 22, 32, and 38 accordingly.

</details>

---

### `examples/hello_world_serial.py:11`

**Type:** nitpick

**Consider using exit code 2 for CLI usage errors.**

Exit code 2 is the Unix convention for command-line usage errors (also used by `argparse`). Exit code 3 works but is less conventional.

<details>
<summary>Suggested change</summary>

```diff
     if len(sys.argv) != 2:
         print(f"usage: {sys.argv[0]} message", file=sys.stderr)
-        raise SystemExit(3)
+        raise SystemExit(2)
```

</details>

---

### `examples/get_hw.py:24`

**Type:** nitpick

**Consider adding defensive attribute access for `my_node_num`.**

If `myInfo` is present but malformed (lacking `my_node_num`), direct attribute access could raise `AttributeError`. Using `getattr` would make this more resilient:

<details>
<summary>♻️ Suggested defensive approach</summary>

```diff
-        if my_info.my_node_num < 0:
+        my_node_num = getattr(my_info, "my_node_num", -1)
+        if my_node_num < 0:
             print("Local node has not joined the mesh yet.", file=sys.stderr)
             raise SystemExit(1)
+
+        nodes_by_num = iface.nodesByNum if isinstance(iface.nodesByNum, dict) else {}
+        node = nodes_by_num.get(my_node_num)
```

</details>

---

### `codecov.yml:106`

**Type:** nitpick

**Low coverage target for Analysis Tools component.**

The analysis component has a 30% coverage target, which is significantly lower than the global 70% target. While this might be intentional for legacy or difficult-to-test code, such low thresholds can lead to under-tested code becoming acceptable. Consider a plan to gradually increase this target over time.

---

### `codecov.yml:84`

**Type:** nitpick

**Low coverage target for Power Monitoring component.**

The powermon component has a 30% coverage target, which is significantly lower than the global 70% target. While this might be intentional for legacy or difficult-to-test code, such low thresholds can lead to under-tested code becoming acceptable. Consider a plan to gradually increase this target over time.

---

### `.trunk/trunk.yaml:82`

**Type:** nitpick

**isort@7.0.0 is outdated.**

The latest isort version is 8.0.1, released Feb 28, 2026. Consider upgrading to get the latest fixes and features, including support for Python 3.14.

<details>
<summary>Suggested update</summary>

```diff
-    - isort@7.0.0
+    - isort@8.0.1
```

</details>

---

### `.trunk/trunk.yaml:56`

**Type:** nitpick

**Pylint `--exit-zero` means checks will never fail.**

The `--exit-zero` flag combined with `success_codes: [0]` means pylint will report issues but never cause `trunk check` to fail. This is useful for advisory reporting, but ensure issues are monitored elsewhere (e.g., CI dashboard) to avoid accumulating technical debt.

---

### `.trunk/trunk.yaml:36`

**Type:** nitpick

**Consider enabling `cache_results` for faster repeated runs.**

The `cache_results: false` setting may slow down incremental lint checks. If mypy's own caching is insufficient, consider enabling Trunk's caching. Also note that the hardcoded `meshtastic/` path makes this definition project-specific, which is fine for this repository.

---

### `.github/copilot-instructions.md:219`

**Type:** nitpick

**Consider adding brief descriptions for less obvious dependencies.**

While most dependencies are self-explanatory, some might benefit from brief inline comments about their purpose (e.g., `dotmap` for dict-like attribute access, `pytap2` for TAP tunnel interface). This would help new contributors understand the project's architecture at a glance.

<details>
<summary>📝 Example enhancement</summary>

```markdown
\### Required

- `pyserial` - Serial port communication
- `protobuf` - Protocol Buffers
- `pypubsub` - Pub/sub messaging
- `bleak` - BLE communication
- `tabulate` - Table formatting for CLI output
- `pyyaml` - YAML config file support
- `requests` - HTTP requests for firmware updates

\### Optional (extras)

- `cli` extra: `pyqrcode` (QR codes), `print-color` (colored output), `dotmap` (dict attribute access), `argcomplete` (shell completion)
- `tunnel` extra: `pytap2` (TAP network interface)
- `analysis` extra: `dash` (web dashboards), `pandas` (data analysis)
```

</details>

---

### `.github/copilot-instructions.md:203`

**Type:** nitpick

**Consider briefly explaining what Trunk is.**

The command `TRUNK_INTERACTIVE=0 .trunk/trunk check --fix --show-existing` is specific but might be unclear to new contributors unfamiliar with Trunk. Consider adding a brief note that Trunk is the project's unified linting and type-checking tool, or reference where developers can learn more about it.

<details>
<summary>📝 Optional clarification</summary>

```diff
-3. Run unified lint/type checks: `TRUNK_INTERACTIVE=0 .trunk/trunk check --fix --show-existing`
+3. Run unified lint/type checks (Trunk runs pylint, Ruff, mypy, etc.): `TRUNK_INTERACTIVE=0 .trunk/trunk check --fix --show-existing`
```

</details>

---
