# REFACTOR_PROGRAM-2.md
### Meshtastic Python Refactor – Phase 2 Program Plan

Date: 2026-03-11  
Base Branch: `develop`  
Comparison Baseline: `master` (eb964d78)

---

# 1. Context

The current `develop` branch represents a large structural modernization of the Meshtastic Python codebase. It includes:

- Codebase modernization
- BLE architecture rewrite
- CLI cleanup
- Compatibility shims
- Major test expansion
- CI pipeline improvements
- Async / concurrency correctness improvements

`develop` is currently **two large commits ahead of `master`**, but those commits contain:

- ~150 files changed
- ~60k insertions
- ~10k deletions

This is effectively a **platform-wide refactor**, not a typical incremental change.

Because of this, the next phase should **not** merge `develop → master` directly.

Instead, we will perform **a small number of large, deliberate stabilization passes** based on `develop`.

These passes will become the next branches and PRs.

---

# 2. Refactor Philosophy Going Forward

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

# 3. Current Refactor State

The repository now contains significant architectural improvements.

### Major Completed Work

#### BLE Architecture Rewrite

The BLE implementation has been modularized into:

```
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

```
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

```
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

# 4. Key Remaining Risks

Despite the progress, several areas remain high risk:

### BLE Lifecycle Behavior

BLE is the most complex subsystem and includes:

- async coordination
- OS-specific behavior
- reconnect logic
- ownership / gating logic
- management operations

The architecture is improved, but lifecycle correctness must be stabilized.

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

# 5. Next Phase Refactor Strategy

Instead of merging `develop → master`, the next steps will consist of **three large stabilization branches**.

Each branch will be created from `develop`.

---

# Phase 1: BLE Stabilization Pass

Branch name suggestion:

```
ble-stabilization-pass
```

This will be the **largest and most important stabilization phase**.

### Goals

- finalize BLE lifecycle behavior
- stabilize connect / disconnect / reconnect logic
- verify state machine transitions
- ensure safe shutdown
- ensure compatibility with legacy BLE APIs

### Areas of Work

BLE lifecycle correctness

- connection ownership rules
- pairing / trust support
- management operation tracking
- reconnect timing behavior
- shutdown ordering

Concurrency review

- locking order
- race conditions
- notification coordination
- state synchronization

Compatibility verification

- historical callbacks
- legacy method names
- BLE public API exports

Testing improvements

- lifecycle edge cases
- reconnect scenarios
- partial connection failures
- shutdown reliability

### Explicitly Out of Scope

- CLI refactor
- mesh core restructuring
- style cleanup unrelated to BLE
- unrelated test rewrites

---

# Phase 2: MeshInterface Runtime Stabilization

Branch name suggestion:

```
meshinterface-runtime-stabilization
```

This phase focuses on correctness of the core networking behavior.

### Goals

- finalize request wait-state semantics
- ensure deterministic callback handling
- prevent late callback contamination
- improve routing error propagation

### Areas of Work

```
meshtastic/mesh_interface.py
meshtastic/node.py
```

Key improvements

- request-scoped wait tracking
- retired wait request handling
- response handler locking validation
- atomic queue send behavior
- routing error timing cleanup

Testing

```
tests/test_mesh_interface.py
```

Add or refine tests covering:

- multiple concurrent requests
- late responses
- cancellation behavior
- transport error handling

### Explicitly Out of Scope

- BLE architecture
- CLI behavior
- host parsing
- OTA logic

---

# Phase 3: Runtime Boundary Cleanup

Branch name suggestion:

```
runtime-boundary-cleanup
```

This phase improves system boundaries and runtime behavior.

### Goals

- unify host/port parsing
- improve runtime error surfaces
- improve OTA error handling
- improve configuration reliability

### Areas of Work

New module usage:

```
meshtastic/host_port.py
```

Responsibilities:

- shared host/port parsing
- IPv4 / IPv6 validation
- CLI / runtime consistency

OTA improvements

```
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

# Phase 4: CLI Structural Refactor (Future)

This phase should **not begin until runtime behavior is stable**.

Branch name suggestion:

```
cli-modularization
```

Goals:

Split `__main__.py` responsibilities into separate modules.

Possible structure:

```
meshtastic/cli/
    commands.py
    connection.py
    config.py
    powermon.py
    export.py
```

The CLI entry point should eventually become minimal:

```
main()
  -> argument parsing
  -> command dispatch
```

---

# 6. Merge Strategy

The final merge into `master` will occur **after phases 1–3 are completed**.

This allows:

- stabilization of BLE
- stabilization of MeshInterface runtime behavior
- stabilization of runtime behavior

Once these subsystems are stable, the refactor can be safely promoted to production.

---

# 7. Development Rules For Next Phases

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

# 8. Immediate Next Step

Create the first stabilization branch from `develop`.

```
git checkout develop
git checkout -b ble-stabilization-pass
```

Focus entirely on:

- BLE lifecycle correctness
- reconnect logic
- shutdown reliability
- compatibility verification
- lifecycle test coverage

This will produce the **next major PR** in the refactor program.

---

# 9. Summary

The major architectural refactor is largely complete.

The next work should not expand scope further.

Instead, the project should proceed through **large stabilization passes**:

1. BLE stabilization
2. MeshInterface runtime stabilization
3. Runtime boundary cleanup
4. CLI modularization (future)

Once these passes are complete, `develop` will be ready to merge into `master`.

This staged approach preserves the benefits of the refactor while reducing integration risk.
