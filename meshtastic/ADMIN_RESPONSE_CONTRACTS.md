# Typed admin response correlation

Firmware 2.8 tracks outgoing admin getter requests and accepts only a matching,
non-replayed response from the requested remote. The Python client mirrors that model
for callback correlation: getter callbacks are bound to the intended source node,
admin response oneof, and config/module subtype where applicable.

A packet carrying the same `request_id` but the wrong source or response shape is
ignored without consuming the pending callback. Routing ACK/NAK handling remains
unchanged, and the two-field public `ResponseHandler` tuple stays backward compatible.
