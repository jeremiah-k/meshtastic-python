# Typed admin response correlation

Firmware 2.8 tracks outgoing admin getter requests and accepts only a matching,
non-replayed response from the requested remote. The Python client mirrors that model
for callback correlation: getter callbacks are bound to the intended source node,
admin response oneof, and config/module subtype where applicable.

A packet carrying the same `request_id` but the wrong source or response shape is
ignored without consuming the pending callback. Routing ACK/NAK handling remains
unchanged, and the two-field public `ResponseHandler` tuple stays backward compatible.

## Firmware 2.8 administrative getters

The firmware 2.8 CLI additions reuse the same typed response-correlation path for
these request/response pairs:

| Request field                          | Expected response field                 |
| -------------------------------------- | --------------------------------------- |
| `get_device_connection_status_request` | `get_device_connection_status_response` |
| `get_ui_config_request`                | `get_ui_config_response`                |

`Node.requestDeviceConnectionStatus()` and `Node.requestUiConfig()` therefore accept
only the named admin response from the requested source. Completion is decided by the
bounded correlated-response wait alone; no routing ACK/NAK wait is performed, and a
routing ACK is never treated as the getter payload. Malformed, wrong-source,
wrong-variant, and replayed packets leave the bounded response wait pending rather
than satisfying the callback.
