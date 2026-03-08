# CodeRabbit Sweep Checklist (maint-35-2)

This document tracks PR-local review triage and execution progress for the
current stabilization sweep.

## Scope

- Branch target: maintenance integration (`maint-35-2` line of work).
- Goal: close race/concurrency/state correctness gaps, then tighten test and
  documentation quality for merge readiness.
- Policy split:
  - In-scope here: correctness and high-confidence quality fixes.
  - Deferred follow-up: broader refactors that materially expand PR risk.

## A) Correctness / Concurrency / State Safety

- [x] BLE connect publish ownership re-check hardening.
- [x] `_client_publish_pending` cleanup on provisional disconnect edge paths.
- [x] Pair/timeout reconnect kwargs propagation in BLE stress reconnect paths.
- [x] Request-scoped wait/error state in `MeshInterface`.
- [x] Send-failure rollback for request wait state/response handlers.
- [x] Request waiter early wake on recorded scoped errors.
- [x] Overlapping wait tracking via `_active_wait_request_ids: dict[str, set[int]]`.
- [x] Unscoped wait-error handling for overlapping request-scoped waits.
- [x] OTA update uses one validated file handle for hash/size + streaming.
- [x] OTA local-target guard in CLI (`--ota-update` rejects remote `--dest`).
- [x] Powermon optional backend import masking narrowed to dependency misses only.
- [x] Dead weakref pruning before id(owner) fallback checks in BLE gating.
- [x] Second-phase BLE gating re-check includes provisional claim state.
- [x] BLE constructor/connect boolean validation for `pair_on_connect` and `pair`.
- [x] Smokevirt host/port mismatch guard and host-port normalization.

## B) Test Reliability / Lane Behavior / Hygiene

- [x] BLE fixture `_StubBleakClient` includes `stop_notify` compatibility shim.
- [x] BLE fixture stubbed connect sets connected state + clears publish-pending.
- [x] BLE stress tests require post-baseline reconnect clients (not stale pass).
- [x] BLE stress tests verify reconnect kwargs preserve caller overrides.
- [x] BLE stress helpers snapshot state under shared lock consistently.
- [x] MeshInterface tests isolate/error-check wait-state behavior.
- [x] OTA progress assertions derive byte counts from constants/payload sizes.
- [x] Powermon tests use direct lazy-export access and monkeypatch-based cleanup.
- [x] meshtasticd TCP reconnect test includes post-close settle delay.
- [x] Smoke destructive-lane retry budget bounded for teardown stability.
- [x] Smoke tests no longer require cleartext wifi_psk echo contract.

## C) API / Docs / Style Alignment

- [x] `_read_bytes()` docstring includes explicit ValueError contract.
- [x] New BLE timeout aliases are explicitly typed.
- [x] Stress-helper return annotations avoid `Any` where required.
- [x] Example config helper constant has explicit public-use note.
- [x] Tunnel privilege warning only logs on TapDevice creation path.
- [x] BLE trust docs reflect optional address (`trust(address=None)`).
- [x] Powermon module exposes lazy symbols in `__dir__`.
- [x] Powermon placeholder backend uses `__new__` failure path (W0231-safe).
- [x] Gating helper docstrings normalized to NumPy-style sections where touched.

## D) Known Deferred Follow-Ups (separate PR candidates)

- [ ] Shared host/port parser extraction used by both Python tests/runtime and
      shell entrypoints (to fully remove parser drift risk).
- [ ] Trunk plugin ref/version pin realignment verification pass.
- [ ] Optional deeper BLE publish path refactor to make provisional client
      entirely invisible to all direct readers before final ownership publish.
- [ ] Potential explicit policy on whether destructive smoke tests should carry
      both `smoke1` and `smoke1_destructive` or destructive-only marker.

## Validation Protocol

1. Run focused suites for touched domains (BLE, mesh waits, OTA, powermon,
   smoke/meshtasticd script-adjacent tests).
2. Run `make ci-strict` once after implementation stabilizes.
3. If strict gate reports regressions, apply targeted fixes and rerun only the
   impacted suites plus required strict checks.
