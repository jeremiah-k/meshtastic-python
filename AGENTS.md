# Meshtastic Python Agent Guide

This document tracks coding standards and API refactoring decisions for the Meshtastic Python library.

## Naming Conventions

### Public API (camelCase)

- Standard public methods should use `camelCase` (e.g., `sendText`, `sendData`).
- BLE promoted camelCase: `findDevice`, `isConnected`, `stopNotify`.
- Powermon promoted camelCase: `getAverageCurrentMA`, `getMinCurrentMA`, `getMaxCurrentMA`, `resetMeasurements`.
- Slog promoted camelCase: `rootDir`, `storeCurrentReading`.

### Compatibility Shims (snake_case or deprecated camelCase)

- Historical BLE `snake_case` names must remain callable: `find_device`, `read_gatt_char`, `start_notify`.
- BLE `snake_case` shims for promoted names: `is_connected`, `stop_notify`.
- Powermon `snake_case` shims: `get_average_current_mA`, `get_min_current_mA`, `get_max_current_mA`, `reset_measurements`.
- Powermon deprecated camelCase: `getAverageCurrentmA`, `getMinCurrentmA`, `getMaxCurrentmA`.
- Slog `snake_case` shims: `root_dir`, `store_current_reading`.

### Internal Helpers (\_snake_case)

- All internal methods and helpers should be prefixed with an underscore and use `snake_case` (e.g., `_send_packet`, `_handle_disconnect`).

## BLE API Refactoring Decisions

Source of truth for BLE compatibility is `master`'s historical `meshtastic/ble_interface.py` public surface.

### Historical BLE public methods from `master` that MUST remain callable

- `BLEClient.async_await`
- `BLEClient.async_run`
- `BLEInterface.from_num_handler`
- `BLEInterface.log_radio_handler`
- `BLEInterface.legacy_log_radio_handler`

These names are kept as compatibility wrappers over canonical internal helpers (`_async_await`, `_async_run`, `_from_num_handler`, `_log_radio_handler`, `_legacy_log_radio_handler`).

### BLE warning policy (explicit)

- **No deprecation warnings** for historical BLE public methods listed above.
- `find_device` is a compatibility shim to `findDevice` and may emit a deprecation warning.
- Do not add deprecation warnings to other BLE compatibility entrypoints unless explicitly approved.

### BLE internal orchestration naming

- Internal handlers and helpers stay `_snake_case`.
- `ReconnectPolicy` is internal orchestration: canonical methods are `next_attempt` and `get_attempt_count` (snake_case).
- Do not treat internal orchestration method names as public compatibility surface.

## Powermon API Refactoring Decisions

| Class        | Method                   | Refactor Action        |
| ------------ | ------------------------ | ---------------------- |
| `PowerMeter` | `getAverageCurrentMA`    | Primary implementation |
| `PowerMeter` | `get_average_current_mA` | Compatibility shim     |
| `PowerMeter` | `getMinCurrentMA`        | Primary implementation |
| `PowerMeter` | `get_min_current_mA`     | Compatibility shim     |
| `PowerMeter` | `getMaxCurrentMA`        | Primary implementation |
| `PowerMeter` | `get_max_current_mA`     | Compatibility shim     |
| `PowerMeter` | `resetMeasurements`      | Primary implementation |
| `PowerMeter` | `reset_measurements`     | Compatibility shim     |

### Powermon warning policy (explicit)

- Deprecated camelCase spellings with lowercase unit suffix (`getAverageCurrentmA`, `getMinCurrentmA`, `getMaxCurrentmA`) emit `DeprecationWarning`.
- Canonical camelCase (`getAverageCurrentMA`, `getMinCurrentMA`, `getMaxCurrentMA`) and snake_case shims do not warn.

## Slog API Refactoring Decisions

| Class         | Method                  | Refactor Action        |
| ------------- | ----------------------- | ---------------------- |
| `PowerLogger` | `_p_meter`              | Internal attribute     |
| `PowerLogger` | `pMeter`                | Public property        |
| `PowerLogger` | `storeCurrentReading`   | Primary implementation |
| `PowerLogger` | `store_current_reading` | Compatibility shim     |
