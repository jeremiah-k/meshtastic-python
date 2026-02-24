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

| Class             | Method                     | Refactor Action                       |
| ----------------- | -------------------------- | ------------------------------------- |
| `BLEInterface`    | `from_num_handler`         | Rename to `_from_num_handler`         |
| `BLEInterface`    | `log_radio_handler`        | Rename to `_log_radio_handler`        |
| `BLEInterface`    | `legacy_log_radio_handler` | Rename to `_legacy_log_radio_handler` |
| `BLEClient`       | `has_characteristic`       | Keep as is (not promoted)             |
| `ReconnectPolicy` | `next_attempt`             | Remove (internal orchestration)       |
| `ReconnectPolicy` | `get_attempt_count`        | Remove (internal orchestration)       |

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

## Slog API Refactoring Decisions

| Class         | Method                  | Refactor Action        |
| ------------- | ----------------------- | ---------------------- |
| `PowerLogger` | `_p_meter`              | Internal attribute     |
| `PowerLogger` | `pMeter`                | Public property        |
| `PowerLogger` | `storeCurrentReading`   | Primary implementation |
| `PowerLogger` | `store_current_reading` | Compatibility shim     |
