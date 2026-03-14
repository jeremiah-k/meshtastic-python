# Compatibility Inventory and Deprecation Matrix

This document is the canonical source of truth for compatibility aliases,
deprecations, and legacy compatibility behaviors in this repository.

If a compatibility symbol is not listed here, do not add or keep it by default.

## Scope and Policy

- Runtime baseline is Python 3.10+.
- Public API names prefer `camelCase`.
- Historical compatibility shims remain callable where documented below.
- Internal helpers prefer underscore-prefixed `snake_case`.
- Naming-only compatibility aliases are silent unless explicitly marked deprecated.
- Naming-only deprecations must be warn-once.
- Semantic deprecations may warn on every invalid usage.

## Status Legend

- `PRIMARY`: canonical symbol for new code.
- `COMPAT_STABLE_SHIM`: maintained compatibility alias; callable and silent.
- `COMPAT_DEPRECATE`: maintained compatibility alias that emits
  `DeprecationWarning` (warn-once unless explicitly noted).
- `SEMANTIC_DEPRECATE`: behavioral migration warning (not naming-only).
- `INTERNAL_COMPAT`: compatibility entrypoint intentionally retained for
  integrations, but not considered public API growth.

## Authoritative Baselines

- BLE historical baseline tag: `2.7.7`
- BLE baseline commit: `b26d80f1866ffa765467e5cb7688c59dee7f2bb2`
- Baseline file: `meshtastic/ble_interface.py`

## BLE Historical Baseline (2.7.7)

The following historical BLE symbols are required compatibility surface and must
remain callable and silent.

| Symbol                                  | Status               | Warning policy | Notes                                  |
| --------------------------------------- | -------------------- | -------------- | -------------------------------------- |
| `BLEClient.async_await`                 | `COMPAT_STABLE_SHIM` | Silent         | Delegates to `_async_await`.           |
| `BLEClient.async_run`                   | `COMPAT_STABLE_SHIM` | Silent         | Delegates to `_async_run`.             |
| `BLEInterface.from_num_handler`         | `COMPAT_STABLE_SHIM` | Silent         | Delegates to `_from_num_handler`.      |
| `BLEInterface.log_radio_handler`        | `COMPAT_STABLE_SHIM` | Silent         | Keep historical `async def` signature. |
| `BLEInterface.legacy_log_radio_handler` | `COMPAT_STABLE_SHIM` | Silent         | Keep historical `async def` signature. |

Additional approved BLE compatibility and promotions:

| Symbol                                    | Status               | Warning policy | Notes                                          |
| ----------------------------------------- | -------------------- | -------------- | ---------------------------------------------- |
| `BLEClient.find_device`                   | `COMPAT_STABLE_SHIM` | Silent         | Historical snake_case.                         |
| `BLEClient.findDevice`                    | `PRIMARY`            | Silent         | Approved promoted camelCase name.              |
| `BLEClient.is_connected`                  | `COMPAT_STABLE_SHIM` | Silent         | Shim for `isConnected`.                        |
| `BLEClient.isConnected`                   | `PRIMARY`            | Silent         | Approved promoted camelCase name.              |
| `BLEClient.stop_notify`                   | `COMPAT_STABLE_SHIM` | Silent         | Shim for `stopNotify`.                         |
| `BLEClient.stopNotify`                    | `PRIMARY`            | Silent         | Approved promoted camelCase name.              |
| `BLEInterface.find_device`                | `COMPAT_STABLE_SHIM` | Silent         | Historical snake_case wrapper.                 |
| `BLEInterface.findDevice`                 | `PRIMARY`            | Silent         | Approved promoted camelCase name.              |
| `BLEClient._discover`                     | `INTERNAL_COMPAT`    | Silent         | Historical internal discovery entrypoint.      |
| `BLEStateManager._lock`                   | `INTERNAL_COMPAT`    | Silent         | Alias to `lock` property for legacy internals. |
| `meshtastic.ble_interface.BleakClient`    | `COMPAT_STABLE_SHIM` | Silent         | Legacy module-level import compatibility.      |
| `meshtastic.ble_interface.BleakScanner`   | `COMPAT_STABLE_SHIM` | Silent         | Legacy module-level import compatibility.      |
| `meshtastic.ble_interface.BLEDevice`      | `COMPAT_STABLE_SHIM` | Silent         | Legacy module-level import compatibility.      |
| `meshtastic.ble_interface.BleakError`     | `COMPAT_STABLE_SHIM` | Silent         | Legacy module-level import compatibility.      |
| `meshtastic.ble_interface.BleakDBusError` | `COMPAT_STABLE_SHIM` | Silent         | Legacy module-level import compatibility.      |

Approved BLE deprecation:

| Symbol                                                      | Status             | Warning policy                | Notes                            |
| ----------------------------------------------------------- | ------------------ | ----------------------------- | -------------------------------- |
| `BLECoroutineRunner._run_coroutine_threadsafe(timeout=...)` | `COMPAT_DEPRECATE` | Warn-once per runner instance | Alias for `startup_timeout=...`. |

## Deprecated Compatibility Aliases

These are intentionally maintained deprecated aliases.

| Symbol                                | Canonical replacement               | Status             | Warning policy                             |
| ------------------------------------- | ----------------------------------- | ------------------ | ------------------------------------------ |
| `mt_config.tunnelInstance`            | `mt_config.tunnel_instance`         | `COMPAT_DEPRECATE` | Warn-once per process (read/write/delete). |
| `util.dotdict`                        | `util.DotDict`                      | `COMPAT_DEPRECATE` | Warn-once per process.                     |
| `slog.root_dir()`                     | `slog.rootDir()`                    | `COMPAT_DEPRECATE` | Warn-once per process.                     |
| `PowerLogger.store_current_reading()` | `PowerLogger.storeCurrentReading()` | `COMPAT_DEPRECATE` | Warn-once per instance.                    |
| `PowerMeter.getAverageCurrentmA()`    | `PowerMeter.getAverageCurrentMA()`  | `COMPAT_DEPRECATE` | Warn-once per process key.                 |
| `PowerMeter.getMinCurrentmA()`        | `PowerMeter.getMinCurrentMA()`      | `COMPAT_DEPRECATE` | Warn-once per process key.                 |
| `PowerMeter.getMaxCurrentmA()`        | `PowerMeter.getMaxCurrentMA()`      | `COMPAT_DEPRECATE` | Warn-once per process key.                 |

Semantic deprecation:

| Behavior                                                                                | Status               | Warning policy               | Notes                                          |
| --------------------------------------------------------------------------------------- | -------------------- | ---------------------------- | ---------------------------------------------- |
| `MeshInterface.sendTelemetry(telemetryType=<unsupported>)` fallback to `device_metrics` | `SEMANTIC_DEPRECATE` | Warn every unsupported input | Behavioral migration warning, not naming-only. |

## Stable Compatibility Aliases (Silent)

### Core Package and CLI

| Module                | Compatibility symbol                    | Canonical symbol                       |
| --------------------- | --------------------------------------- | -------------------------------------- |
| `meshtastic.__init__` | `meshtastic.serial` (lazy module alias) | third-party `serial` (pyserial) module |
| `meshtastic.__main__` | `support_info()`                        | `supportInfo()`                        |
| `meshtastic.__main__` | `export_config`                         | `exportConfig`                         |
| `meshtastic.__main__` | `create_power_meter`                    | `_create_power_meter`                  |
| `meshtastic.__main__` | `_PREFERENCE_FIELD_ALIASES` legacy keys | canonical protobuf preference names    |
| `meshtastic.version`  | `get_active_version()`                  | `getActiveVersion()`                   |
| `meshtastic.test`     | `subscribe()`                           | `subscribeToNodeUpdates()`             |

`_PREFERENCE_FIELD_ALIASES` currently normalizes:

- `display.use_12_hour -> display.use_12h_clock`
- `display.use12_hour -> display.use_12h_clock`
- `display.use12h_clock -> display.use_12h_clock`
- `display.use12_h_clock -> display.use_12h_clock`

### Utility and Config

| Module                 | Compatibility symbol      | Canonical symbol       |
| ---------------------- | ------------------------- | ---------------------- |
| `meshtastic.host_port` | `parse_host_and_port()`   | `parseHostAndPort()`   |
| `meshtastic.util`      | `blacklistVids`           | `BLACKLIST_VIDS`       |
| `meshtastic.util`      | `whitelistVids`           | `WHITELIST_VIDS`       |
| `meshtastic.util`      | `our_exit()`              | `ourExit()`            |
| `meshtastic.util`      | `remove_keys_from_dict()` | `removeKeysFromDict()` |
| `meshtastic.util`      | `detect_windows_port()`   | `detectWindowsPort()`  |
| `meshtastic.util`      | `message_to_json()`       | `messageToJson()`      |
| `meshtastic.util`      | `to_node_num()`           | `toNodeNum()`          |
| `meshtastic.util`      | `flags_to_list()`         | `flagsToList()`        |

### Node and Tunnel

| Module                      | Compatibility symbol                                   | Canonical symbol                        |
| --------------------------- | ------------------------------------------------------ | --------------------------------------- |
| `meshtastic.mesh_interface` | `_generatePacketId()`                                  | `_generate_packet_id()`                 |
| `meshtastic.mesh_interface` | `_sendPacket()`                                        | `_send_packet()`                        |
| `meshtastic.node`           | `position_flags_list()`                                | `positionFlagsList()`                   |
| `meshtastic.node`           | `excluded_modules_list()`                              | `excludedModulesList()`                 |
| `meshtastic.node`           | `module_available()`                                   | `moduleAvailable()`                     |
| `meshtastic.node`           | `get_ringtone()`                                       | `getRingtone()`                         |
| `meshtastic.node`           | `set_ringtone()`                                       | `setRingtone()`                         |
| `meshtastic.node`           | `get_canned_message()`                                 | `getCannedMessage()`                    |
| `meshtastic.node`           | `set_canned_message()`                                 | `setCannedMessage()`                    |
| `meshtastic.node`           | `get_channels_with_hash()`                             | `getChannelsWithHash()`                 |
| `meshtastic.node`           | `startOTA(ota_mode=..., ota_hash=..., hash=...)`       | `startOTA(mode=..., ota_file_hash=...)` |
| `meshtastic.node`           | live-channel semantics of `getChannelByChannelIndex()` | historical mutate-then-write behavior   |
| `meshtastic.tunnel`         | `udpBlacklist`                                         | `UDP_BLACKLIST`                         |
| `meshtastic.tunnel`         | `tcpBlacklist`                                         | `TCP_BLACKLIST`                         |
| `meshtastic.tunnel`         | `protocolBlacklist`                                    | `PROTOCOL_BLACKLIST`                    |
| `meshtastic.tunnel`         | `_shouldFilterPacket()`                                | `_should_filter_packet()`               |
| `meshtastic.tunnel`         | `_ipToNodeId()`                                        | `_ip_to_node_id()`                      |
| `meshtastic.tunnel`         | `_nodeNumToIp()`                                       | `_node_num_to_ip()`                     |
| `meshtastic.tunnel`         | `sendPacket()`                                         | `_send_packet()`                        |

### BLE and Related Exports

| Module                                | Compatibility symbol         | Canonical symbol              |
| ------------------------------------- | ---------------------------- | ----------------------------- |
| `meshtastic.interfaces.ble.client`    | `find_device()`              | `discover()`                  |
| `meshtastic.interfaces.ble.client`    | `_discover()`                | `discover()`                  |
| `meshtastic.interfaces.ble.client`    | `is_connected()`             | `isConnected()`               |
| `meshtastic.interfaces.ble.client`    | `stop_notify()`              | `stopNotify()`                |
| `meshtastic.interfaces.ble.client`    | `async_await()`              | `_async_await()`              |
| `meshtastic.interfaces.ble.client`    | `async_run()`                | `_async_run()`                |
| `meshtastic.interfaces.ble.discovery` | `_discover_devices()`        | `discover_devices()`          |
| `meshtastic.interfaces.ble.interface` | `find_device()`              | `findDevice()`                |
| `meshtastic.interfaces.ble.interface` | `from_num_handler()`         | `_from_num_handler()`         |
| `meshtastic.interfaces.ble.interface` | `log_radio_handler()`        | `_log_radio_handler()`        |
| `meshtastic.interfaces.ble.interface` | `legacy_log_radio_handler()` | `_legacy_log_radio_handler()` |
| `meshtastic.interfaces.ble.state`     | `_lock` property             | `lock` property               |

### Powermon and Slog

| Module                                        | Compatibility symbol             | Canonical symbol                                                       |
| --------------------------------------------- | -------------------------------- | ---------------------------------------------------------------------- |
| `meshtastic.powermon.power_supply.PowerMeter` | `get_average_current_mA()`       | `getAverageCurrentMA()`                                                |
| `meshtastic.powermon.power_supply.PowerMeter` | `get_min_current_mA()`           | `getMinCurrentMA()`                                                    |
| `meshtastic.powermon.power_supply.PowerMeter` | `get_max_current_mA()`           | `getMaxCurrentMA()`                                                    |
| `meshtastic.powermon.power_supply.PowerMeter` | `reset_measurements()`           | `resetMeasurements()`                                                  |
| `meshtastic.powermon.ppk2.PPK2PowerSupply`    | `reset_measurements()`           | `resetMeasurements()`                                                  |
| `meshtastic.powermon.riden.RidenPowerSupply`  | `get_average_current_mA()`       | `getAverageCurrentMA()`                                                |
| `meshtastic.powermon.riden.RidenPowerSupply`  | `_getRawWattHour()`              | `_get_raw_watt_hour()`                                                 |
| `meshtastic.powermon.riden.RidenPowerSupply`  | `nowWattHour` attribute          | `now_watt_hour`-style internal value not introduced (legacy preserved) |
| `meshtastic.powermon.sim.SimPowerSupply`      | `get_average_current_mA()`       | `getAverageCurrentMA()`                                                |
| `meshtastic.powermon.stress`                  | `handle_power_stress_response()` | `handlePowerStressResponse()`                                          |
| `meshtastic.powermon.stress`                  | `onPowerStressResponse()`        | `handlePowerStressResponse()`                                          |
| `meshtastic.slog.arrow.ArrowWriter`           | `set_schema()`                   | `setSchema()`                                                          |
| `meshtastic.slog.arrow.ArrowWriter`           | `add_row()`                      | `addRow()`                                                             |
| `meshtastic.slog.slog`                        | `p_meter` property               | `pMeter` property                                                      |
| `meshtastic.slog.slog`                        | `_onLogMessage()`                | `_on_log_message()`                                                    |

Slog schema compatibility fields retained in `PowerLogger` rows and Arrow schema:

- `average_mW` (legacy alias field)
- `max_mW` (legacy alias field)
- `min_mW` (legacy alias field)

Preferred fields for current values are `average_mA`, `max_mA`, and `min_mA`.

### Other Modules

| Module                                        | Compatibility symbol | Canonical symbol  |
| --------------------------------------------- | -------------------- | ----------------- |
| `meshtastic.analysis.__main__`                | `to_pmon_names()`    | `toPmonNames()`   |
| `meshtastic.ota.ESP32WiFiOTA`                 | `hash_bytes()`       | `hashBytes()`     |
| `meshtastic.ota.ESP32WiFiOTA`                 | `hash_hex()`         | `hashHex()`       |
| `meshtastic.remote_hardware`                  | `onGpioReceive()`    | `onGPIOReceive()` |
| `meshtastic.remote_hardware`                  | `onGPIOreceive()`    | `onGPIOReceive()` |
| `meshtastic.supported_device.SupportedDevice` | `usb_ids` property   | `usbIds` property |

## Non-Public and Boundary Rules

- Symbols under `meshtastic/interfaces/ble/*` are internal by default unless
  explicitly exported via:
  - `meshtastic/ble_interface.py`, or
  - `meshtastic/interfaces/ble/__init__.py`.
- Do not add compatibility aliases for internal orchestration helpers
  (`runner`, `policies`, `coordination`, etc.) without explicit approval.
- `ReconnectPolicy` canonical names remain `next_attempt` and
  `get_attempt_count` (snake_case).
- `meshtastic.interfaces.ble.runner.get_zombie_runner_count()` remains
  internal snake_case-only.

## Maintenance Checklist

When adding/changing compatibility behavior:

1. Update or add the code path with explicit `COMPAT_STABLE_SHIM` or
   `COMPAT_DEPRECATE` marker where appropriate.
2. Update this document in the same change.
3. Add/adjust tests for callability and warning behavior.
4. Run compatibility inventory grep and verify changes:
   - `rg -n "COMPAT_STABLE_SHIM|COMPAT_DEPRECATE" meshtastic`
5. Keep `.github/workflows/ci.yml` compatibility validation green:
   - inventory marker check (`rg -n "COMPAT_STABLE_SHIM|COMPAT_DEPRECATE" meshtastic`)
   - compatibility-focused pytest targets for alias callability and warning behavior
6. Run full project checks as documented in `CONTRIBUTING.md`.
