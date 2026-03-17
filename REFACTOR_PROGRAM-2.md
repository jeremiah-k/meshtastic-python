# Meshtastic Python Refactor Program 2

## Boundary Cleanup and Complexity Reduction Plan

Date: 2026-03-14
Base branch: `develop`
Execution branch: active working branches per workstream

---

## 1) Purpose

This document is the active high-level plan for the next refactor round.

Phases 1-3 from the previous continuation stream are complete and are now
summarized in `REFACTOR_PROGRAM.md` (Section 18). This file is intentionally
reused as the single active continuation plan to avoid plan-doc sprawl.

Primary objective for this round:

- reduce complexity concentration in core runtime objects,
- enforce stronger subsystem boundaries,
- keep compatibility behavior intact while making internals easier to change.

---

## 2) Operating Constraints

### 2.1 First-pass rule

For the highest-risk hotspots, the first pass is **pure refactor**:

- no intentional behavior changes,
- existing public entrypoints remain,
- old entrypoints delegate to extracted internals,
- pubsub topics and compatibility semantics remain stable.

### 2.2 Boundary rule

Cross-component private access is disallowed outside the defining module.

Examples to eliminate:

- `other_component._private_attr`
- `other_component._private_method(...)`

Required direction:

- expose required behavior through explicit collaborator APIs,
- keep private internals private to each module.

### 2.3 Locking rule

Keep lock ownership simple and explicit:

- one primary state lock model per subsystem,
- snapshot-under-lock then perform I/O/callback work after release,
- add lock invariant helpers in debug/test paths where useful.

---

## 3) Current Hotspots (Centers of Gravity)

Highest pressure files:

- `meshtastic/interfaces/ble/interface.py`
- `meshtastic/mesh_interface.py`
- `meshtastic/node.py`
- `meshtastic/__main__.py`

These still carry too many responsibilities (state transitions, rollback,
compatibility, dispatching, rendering, and policy decisions).

---

## 4) Workstreams and Deliverables

### Workstream A: BLE Boundary Enforcement and Decomposition

Goals:

- remove cross-collaborator private access,
- split BLE interface responsibilities into internal services.

Target decomposition:

- lifecycle/connect-disconnect service,
- receive-loop/recovery service,
- management-command service (pair/unpair/trust),
- compatibility/event publication adapter.

Acceptance criteria:

- no external `component._private_*` reaches across BLE collaborators,
- behavior preserved by existing BLE regression tests,
- public BLE compatibility surface unchanged.

---

### Workstream B: MeshInterface Dispatch and State-Split

Goals:

- reduce centrality of `_handle_from_radio()` and related switchboard logic,
- separate waiting-state, queueing, dispatch, and node DB concerns.

Target decomposition:

- inbound radio field dispatch map,
- request/response wait-state service,
- send queue and packet bookkeeping service,
- node DB mutation helpers.

Acceptance criteria:

- `_handle_from_radio()` becomes orchestration/delegation,
- per-field handlers have narrow contracts,
- pubsub topic names and runtime behavior remain stable.

---

### Workstream C: Node Transaction Extraction

Goals:

- remove transaction-engine density from `Node.setURL()`,
- preserve transactional behavior and rollback semantics.

Target decomposition:

- channel transaction planner,
- apply executor,
- rollback executor,
- local cache update/fallback strategy.

Acceptance criteria:

- `Node.setURL()` remains as orchestration,
- extracted transaction object(s) carry rollback/apply detail,
- existing regression and integration tests continue to pass.

---

### Workstream D: CLI Decomposition

Goals:

- shrink `__main__.py` and separate policy from execution.

Target decomposition:

- parser construction,
- option normalization/compat alias translation,
- action execution,
- output/rendering.

Acceptance criteria:

- `__main__.py` is entrypoint and dispatch only,
- CLI behavior remains backward compatible unless explicitly documented,
- tests reorganized by behavior domain.

---

### Workstream E: Compatibility Layer Isolation

Goals:

- move compatibility shim logic out of hot runtime paths where possible,
- keep canonical implementation paths easier to review and change.

Deliverables:

- dedicated compat modules/adapters per subsystem where practical,
- one-line delegating shims for naming-only aliases,
- compatibility inventory updates in `COMPATIBILITY.md`.

Acceptance criteria:

- compatibility behavior remains intact,
- cognitive load in core runtime modules is reduced,
- shim intent remains grep-visible via `COMPAT_*` markers.

---

### Workstream F: Reliability and Suppression Hygiene

Goals:

- review broad exception catches in reliability-critical paths,
- document intent and reduce accidental suppression risk.

Catch taxonomy:

- cleanup-only,
- telemetry-only,
- rollback-only,
- user-facing recovery,
- last-resort.

Acceptance criteria:

- broad catches are intentionally documented and justified,
- reasoned logging exists for suppressed paths,
- exceptions are narrowed where safe.

---

### Workstream G: Test Architecture and CI Shape Cleanup

Goals:

- reduce mega-test-file pressure,
- keep tests aligned to contracts rather than source-file size.

Deliverables:

- split oversized BLE/CLI tests by behavior domain,
- clarify `meshtastic/tests/` vs top-level `tests/` ownership,
- define fast required lane vs heavier integration lane intent in CI docs.

Acceptance criteria:

- smaller reviewable test modules,
- reduced navigation cost,
- no coverage regressions in touched behavior areas.

---

## 5) Priority Order (Execution)

Highest ROI sequence for near-term passes:

1. stop cross-component private access in BLE,
2. decompose `BLEInterface` internals,
3. refactor `MeshInterface._handle_from_radio()` into helpers/dispatch,
4. extract `Node.setURL()` transaction machinery,
5. isolate compatibility shims from core runtime paths,
6. split oversized BLE/CLI tests and clarify test-directory boundaries,
7. finalize CLI and CI lane intent cleanup.

---

## 6) Tracking Model

Track work as a small number of themed passes (not micro-passes):

- one clear subsystem theme per branch,
- behavior-preserving extraction first,
- focused regression tests for each pass,
- avoid style-only churn mixed with structural work.

Status table (maintained as work lands):

| Workstream                       | Status  | Branch/PR |
| -------------------------------- | ------- | --------- |
| A: BLE boundaries/decomposition  | Planned | TBD       |
| B: MeshInterface dispatch split  | Planned | TBD       |
| C: Node transaction extraction   | Planned | TBD       |
| D: CLI decomposition             | Planned | TBD       |
| E: Compatibility isolation       | Planned | TBD       |
| F: Exception/suppression hygiene | Planned | TBD       |
| G: Test/CI shape cleanup         | Planned | TBD       |

---

## 7) Definition of Done for This Program

This round is complete when:

- complexity concentration in the four hotspot files is materially reduced,
- subsystem boundaries are explicit and enforced,
- compatibility behavior remains stable by default,
- test suites are easier to navigate and maintain,
- CI intent (fast required vs heavy integration) is clear.
