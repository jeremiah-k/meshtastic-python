# REFACTOR_PROGRAM-2.md

## Meshtastic Python Refactor – Phase 2 Program Plan

Date: 2026-03-11  
Base Branch: `develop`  
Comparison Baseline: `master` (eb964d78)

---

## 1. Context

The current `develop` branch represents a large structural modernization of the Meshtastic Python codebase. It includes:

- Codebase modernization
- BLE architecture rewrite
- CLI cleanup
- Compatibility shims
- Major test expansion
- CI pipeline improvements
- Async / concurrency correctness improvements

`develop` is currently **seven commits ahead of `master`** (`master...develop = 0/7`), and the branch delta contains:

- 153 files changed
- 61,115 insertions
- 10,739 deletions

This is effectively a **platform-wide refactor**, not a typical incremental change.

Because of this, we still should **not** merge `develop → master` directly.

Instead, we perform **a small number of large, deliberate stabilization passes** based on `develop`.

Phase 1 (BLE stabilization) is now complete and merged into `develop` (`#63`), and Phase 2 is the active execution stream.

---

## 2. Refactor Philosophy Going Forward

Going forward we will keep the advantages of automation but shift toward **intentional large passes** that address a single subsystem at a time.

### Preferred approach

• **Large thematic passes**  
• **Clean branch histories**  
• **PRs that represent a coherent engineering change**

### What we will avoid

• dozens of tiny PRs  
• style-only churn mixed with behavior changes  
• interleaving multiple subsystem refactors

Each branch should represent **one engineering theme**.

---

## 3. Current Refactor State

The repository now contains significant architectural improvements.

### Major Completed Work

#### BLE Architecture Rewrite

The BLE implementation has been modularized into:

```text
meshtastic/interfaces/ble/
    client.py
    connection.py
    constants.py
    coordination.py
    discovery.py
    errors.py
    gating.py
    interface.py
    notifications.py
    policies.py
    reconnection.py
    runner.py
    state.py
    utils.py
```

A compatibility shim remains at:

```text
meshtastic/ble_interface.py
```

This preserves the historical public API.

---

#### MeshInterface Runtime Improvements

Major improvements include:

- request-scoped wait tracking
- response handler locking
- atomic send queue behavior
- safer shutdown semantics
- race-condition mitigation

These changes significantly improve runtime correctness.

---

#### CLI Improvements

The CLI code received cleanup and better separation of responsibilities.

However:

```text
meshtastic/__main__.py
```

remains **very large (~3200 lines)** and will require future modularization.

---

#### Testing Improvements

The refactor added:

- BLE lifecycle tests
- advanced BLE behavior tests
- mesh interface correctness tests
- CLI behavior tests
- meshtasticd integration tests

This dramatically improves safety for future refactors.

---

## 4. Key Remaining Risks

Despite the progress, several areas remain high risk:

### BLE Lifecycle Behavior

Phase 1 delivered targeted BLE lifecycle hardening and timeout robustness. Remaining BLE risk is now primarily **regression risk**, not major architecture uncertainty.

Residual focus areas:

- cross-platform runtime behavior in real hardware environments
- long-duration reconnect reliability
- maintenance of compatibility semantics as future changes land

---

### MeshInterface Runtime Correctness

The mesh interface now includes sophisticated request tracking and callbacks.

While improvements were made, this area remains critical to stability.

---

### CLI Size and Maintainability

`__main__.py` still contains too many responsibilities.

Future work should split this module once runtime behavior is stable.

---

### Compatibility Surface

Compatibility shims exist but should be validated carefully to ensure:

- historical imports still work
- public API names remain stable
- CLI behavior is preserved where expected

---

## 5. Execution Status and Next-Phase Strategy

Instead of merging `develop → master` directly, the program continues through themed stabilization passes.

### Phase Status Snapshot

- Phase 1 BLE stabilization: **Completed** (merged into `develop`, PR `#63`)
- Phase 2 MeshInterface runtime stabilization: **In progress** on `meshinterface-pass`
- Phase 3 Runtime boundary cleanup: Pending
- Phase 4 CLI structural refactor: Future

---

## Phase 1: BLE Stabilization Pass (Completed)

Executed branch:

```text
ble-stabilization-pass
```

Delivered outcomes:

- elapsed-time-based management wait timeout accounting for connect/shutdown paths
- hardened management operation accounting (underflow-safe behavior)
- bounded spurious-wakeup regression tests to avoid suite hangs
- compatibility-preserving fixes validated by expanded BLE lifecycle tests

---

## Phase 2: MeshInterface Runtime Stabilization (Active)

Active branch:

```text
meshinterface-pass
```

This phase focuses on runtime correctness of request/wait behavior, callback handling, queue behavior, and shared-state safety.

### Phase 2 Goals

- finalize request wait-state semantics
- ensure deterministic callback handling
- prevent late callback contamination
- improve routing error propagation
- enforce lock ownership discipline in mesh runtime paths

### Phase 2 Areas of Work

```text
meshtastic/mesh_interface.py
meshtastic/node.py
meshtastic/tests/test_mesh_interface.py
meshtastic/tests/test_node.py
```

Execution checklist for this phase:

- verify behavior against `master` for backward compatibility and completeness
- preserve documented compatibility aliases from `COMPATIBILITY.md`
- enforce `AGENTS.md` lock-partitioning and shutdown contracts
- harden wait/error bookkeeping for stale, late, and concurrent responses
- add focused tests for real failure modes (timeouts, race windows, stale callbacks)

### Explicitly Out of Scope

- BLE architecture changes (unless required by critical regression)
- CLI behavior or modularization
- host parsing and OTA boundary cleanup work (Phase 3 scope)

---

## Phase 3: Runtime Boundary Cleanup

Branch name suggestion:

```text
runtime-boundary-cleanup
```

This phase improves system boundaries and runtime behavior.

### Phase 3 Goals

- unify host/port parsing
- improve runtime error surfaces
- improve OTA error handling
- improve configuration reliability

### Phase 3 Areas of Work

New module usage:

```text
meshtastic/host_port.py
```

Responsibilities:

- shared host/port parsing
- IPv4 / IPv6 validation
- CLI / runtime consistency

OTA improvements

```text
meshtastic/ota.py
```

Goals:

- clearer transport errors
- better destination validation
- improved socket failure handling

Security improvements

- secret redaction
- preference sanitization
- validation of request IDs

---

## Phase 4: CLI Structural Refactor (Future)

This phase should **not begin until runtime behavior is stable**.

Branch name suggestion:

```text
cli-modularization
```

Goals:

Split `__main__.py` responsibilities into separate modules.

Possible structure:

```text
meshtastic/cli/
    commands.py
    connection.py
    config.py
    powermon.py
    export.py
```

The CLI entry point should eventually become minimal:

```text
main()
  -> argument parsing
  -> command dispatch
```

---

## 6. Merge Strategy

The final merge into `master` will occur **after Phase 2 and Phase 3 are completed** (Phase 1 is already complete).

This allows:

- finalized BLE stability from Phase 1
- validated MeshInterface runtime stabilization from Phase 2
- runtime boundary hardening from Phase 3

Once these subsystems are stable, the refactor can be safely promoted to production.

---

## 7. Development Rules For Next Phases

To keep the refactor manageable:

### Each branch must

- focus on one subsystem
- contain coherent changes
- include tests validating behavior
- avoid mixing unrelated improvements

### Avoid

- style-only commits
- formatting churn
- opportunistic refactors outside the branch theme

### Prefer

- squashable branch histories
- logically grouped commits
- tests that target real failure modes

---

## 8. Immediate Next Step

Continue executing Phase 2 on the active branch:

```bash
git checkout meshinterface-pass
```

Immediate focus:

- complete mesh runtime hardening in `mesh_interface.py` and `node.py`
- run explicit backward-compatibility checks against `master`
- validate compatibility inventory alignment in `COMPATIBILITY.md`
- keep tests green with expanded targeted regression coverage

This work stream should produce the next major PRs for the refactor program.

---

## 9. Summary

The major architectural refactor is largely complete.

The next work should not expand scope further.

Instead, the project proceeds through **large stabilization passes**:

1. BLE stabilization (completed)
2. MeshInterface runtime stabilization (active)
3. Runtime boundary cleanup (pending)
4. CLI modularization (future)

Once these passes are complete, `develop` will be ready to merge into `master`.

This staged approach preserves the benefits of the refactor while reducing integration risk.
