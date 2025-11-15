# Phase 3 – Mesh/Stream/TCP Modernization Plan

This document captures the follow-on work that sits outside the current BLE-focused PR but was surfaced during the latest code review. These items will be tackled when we pivot to Phase 3 (mesh/stream/tcp parity) so they remain visible without bloating the BLE patch set.

## Mesh Interface Cleanups
- Add an explicit type hint (`Union[bytes, bytearray]`) to `mesh_interface._handleFromRadio` to align the signature with its documented inputs and improve lint/type-check coverage.
- Evaluate whether the warning that logs the entire malformed payload should truncate or downgrade to `DEBUG` to avoid excessive log volume on noisy radios.
- Formalize the connected/disconnected event guarantees around `_connected()`/`_disconnected()` once BLE publishes connected status from the orchestration layer; document the contract so Matrix/mmrelay consumers can rely on it.

## Stream/TCP Roadmap
- Audit `stream_interface.py` and `tcp_interface.py` for opportunities to reuse the BLE abstractions (state manager, retry policies, notification tracking) and list the specific touchpoints where parity would benefit reliability.
- Capture any additional logging/observability hooks needed for non-BLE transports so we can prioritize them alongside BLE metrics.

## Test Suite Enhancements
- Track a follow-up to add an integration-style test that exercises the full connection lifecycle (BLE connect → `_connected()` → connection events) once `_startConfig` can be safely invoked inside the fixtures.
- Revisit the stress tests to ensure we have consistent coverage for both connected=True and connected=False events across transports.

## Housekeeping Before Upstreaming
- Remove or relocate temporary planning documents (`ble-1114-1-plan.md`, this file) before merging upstream so the tree remains clean; alternatively, move any persistent content into a developer guide.
- Summarize the finalized BLE architectural changes and any mesh/stream TODOs in release notes or docs so reviewers have a single reference when Phase 3 PRs open.
