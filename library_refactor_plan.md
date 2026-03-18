# AGOR Architectural Refactor Program
## Coordinator Directive (High-Level Execution Plan)

## Execution Progress

- Program status: **PASS 1 Complete (BLE ownership transfer)**
- Last updated: **2026-03-17 (receive/management runtime static-core retirement follow-through)**

### Progress Log

#### PASS 1 — BLE Ownership Refactor (Complete)

- Completed ownership-transfer scope:
  - Converted BLE runtime orchestration to instance-bound collaborators:
    - `BLEManagementCommandHandler`
    - `BLENotificationDispatcher`
    - `BLELifecycleController`
    - `BLEReceiveRecoveryController`
    - `BLECompatibilityEventPublisher`
  - Completed lifecycle ownership split in `lifecycle_service.py`:
    - `BLEReceiveLifecycleCoordinator`
    - `BLEDisconnectLifecycleCoordinator`
    - `BLEConnectionOwnershipLifecycleCoordinator`
    - `BLEShutdownLifecycleCoordinator`
  - Collapsed lifecycle service-core duplication by turning remaining heavy
    static entrypoints into compatibility wrappers that delegate to collaborator
    implementations.
  - Removed direct collaborator-private reach-through from `BLEInterface` for
    state and notification runtime paths.
  - Preserved monkeypatch-heavy compatibility surfaces by keeping static helper
    shim entrypoints callable for tests/integrations while routing behavior
    through bound collaborators.
  - Completed strict runtime authority retirements in this tranche:
    - `BLELifecycleController.close()` now executes collaborator-owned shutdown
      logic directly (`BLEShutdownLifecycleCoordinator.close()`), with
      `BLELifecycleService._close()` retained as compatibility shim.
    - `BLEReceiveRecoveryController.recover_receive_thread()` is now the
      runtime owner for receive-thread recovery flow.
    - `BLEReceiveRecoveryController.receive_from_radio_impl()` is now the
      runtime owner for receive-loop execution flow.
    - `BLEManagementCommandHandler.execute_management_command()` is now the
      runtime owner for management startup/target/gate/client execution.
  - Added compatibility fallback hooks in receive/management collaborators so
    instance monkeypatch overrides used by existing tests/integrations continue
    to work without re-centralizing runtime ownership.

- Architectural outcomes:
  - `BLEInterface` runtime paths now delegate through collaborator APIs instead
    of static service-style namespaces.
  - Ownership domains are explicit across management, notification, lifecycle,
    receive-recovery, and compatibility publication.
  - Static service methods are not the primary runtime authority for
    `BLEInterface` call paths; they remain compatibility surfaces (including
    some intentionally non-trivial direct service entrypoints used by
    compatibility-targeted tests).

- Validation completed:
  - `ruff check meshtastic/interfaces/ble/lifecycle_service.py meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/management_service.py meshtastic/interfaces/ble/notifications.py`: passed.
  - `poetry run pytest -q tests/test_ble_interface_core.py tests/test_ble_utils_service_targets.py tests/test_ble_lifecycle_receive_targets.py tests/test_ble_connection_discovery_client_targets.py`: **264 passed**.
  - `poetry run pytest -q`: **1454 passed, 3 skipped, 92 deselected**.

#### PASS 1 — Follow-through Update (Current)

- Additional ownership transfer completed:
  - `BLEReceiveRecoveryService` runtime-heavy static entrypoints now delegate to `BLEReceiveRecoveryController` owners.
  - Removed static receive-path direct interface authority calls (`iface.close()`, `iface._recover_receive_thread(...)`) from receive runtime entrypoints.
  - `BLEManagementCommandHandler` now owns management timeout/connect-timeout validation, trust command execution, and trust lifecycle flow.
  - `BLEManagementCommandsService` static entrypoints now route through handler-owned runtime implementations.
- Validation completed for this tranche:
  - `ruff check meshtastic/interfaces/ble/receive_service.py meshtastic/interfaces/ble/management_service.py tests/test_ble_lifecycle_receive_targets.py tests/test_ble_utils_service_targets.py`: passed.
  - `pytest -q tests/test_ble_lifecycle_receive_targets.py tests/test_ble_utils_service_targets.py tests/test_ble_interface_core.py -k "receive or management or safe_execute or retry_policy or publishing_thread or malformed_fromnum"`: **110 passed, 135 deselected**.

You are operating as a project coordinator responsible for executing a **large-scale architectural refinement program** on an existing codebase.

This program is **not feature development**. It is a **structural, architectural, and maintainability improvement initiative**.

You may execute work yourself or delegate to other agents. You are responsible for:
- Planning each pass
- Ensuring cohesion of changes
- Verifying correctness
- Producing a structured report after each pass

---

# Core Principles

## 1. Large, Focused Passes
- Work must be executed in **large, cohesive chunks**
- Avoid micro-iterations and small scattered edits
- Each pass should significantly improve one major aspect of the system

## 2. No Behavioral Changes
- Do NOT change public behavior
- Do NOT break compatibility
- Do NOT change APIs, event semantics, or outputs unless explicitly required

## 3. Extract and Clarify Ownership
- Each subsystem should have:
  - clear ownership
  - minimal external knowledge
  - well-defined responsibilities

## 4. Reduce Complexity Concentration
- Eliminate “god objects”
- Reduce central orchestration pressure
- Push logic into well-defined collaborators

## 5. No Shortcut Refactors
- Do NOT create thin wrappers that still rely on original structures
- Refactors must result in **real separation**, not cosmetic movement

---

# Program Overview

The system has evolved significantly but still suffers from:

- Centralized orchestration logic
- Cross-component private access
- Overloaded classes (BLEInterface, MeshInterface, Node, CLI)
- Interwoven compatibility logic
- Large, hard-to-maintain test files

This program addresses those issues in **structured passes**.

---

# PASS STRUCTURE (MANDATORY)

Each pass must follow this structure:

## 1. Define Scope
- What subsystem or concern is being addressed
- Why it is important

## 2. Execute Refactor
- Perform structural changes only
- Maintain behavior
- Improve boundaries and ownership

## 3. Internal Validation
- Ensure code compiles logically
- Ensure no broken references
- Ensure compatibility paths still function

## 4. Report (REQUIRED)
Each pass MUST end with a structured report containing:

### Summary
- What was changed

### Key Improvements
- Architectural gains
- Complexity reduction

### Risks / Tradeoffs
- Any introduced risks
- Areas needing future attention

### Next Recommended Pass
- What should be done next and why

---

# PRIORITY WORKSTREAMS

## PASS 1 — BLE Ownership Refactor (Highest Priority)

### Goal
Convert BLE services from procedural helpers into real collaborators.

### Required Changes
- Replace static-style service usage with instantiated objects
- Move state ownership into appropriate collaborators
- Remove passing of entire interface object into services
- Enforce clean public APIs between components

### Critical Rule
NO cross-component private member access.

---

## PASS 2 — BLE Interface Decomposition

### Goal
Reduce BLEInterface into a thin orchestration layer.

### Required Changes
Extract responsibilities into:
- lifecycle controller
- receive controller
- management command handler
- notification dispatcher
- compatibility publisher

BLEInterface should:
- delegate
- enforce policy
- expose public API

---

## PASS 3 — Mesh Interface Dispatch Refactor

### Goal
Break apart central radio handling logic.

### Required Changes
- Replace monolithic dispatch with handler map
- Separate:
  - parsing
  - normalization
  - state mutation
  - event publication

---

## PASS 4 — Node Transaction Extraction

### Goal
Remove complex transactional logic from Node class.

### Required Changes
- Extract channel update transaction object
- Move rollback and staging logic out of Node
- Keep Node as orchestration layer

---

## PASS 5 — CLI Decomposition

### Goal
Separate CLI concerns.

### Required Changes
Split into:
- parser
- actions
- compatibility layer
- rendering/output

---

## PASS 6 — Compatibility Layer Isolation

### Goal
Remove compatibility logic from core runtime paths.

### Required Changes
- Move shims into dedicated modules
- Ensure canonical paths remain clean
- Reduce inline compatibility branching

---

## PASS 7 — Test Suite Restructuring

### Goal
Improve maintainability of tests.

### Required Changes
- Split large test files by behavior domain
- Preserve coverage
- Improve readability and isolation

---

## PASS 8 — Exception Handling Audit

### Goal
Improve reliability and diagnosability.

### Required Changes
- Classify all broad exception handling
- Narrow where possible
- Add explicit intent (cleanup, fallback, etc.)

---

# EXECUTION RULES

- Do NOT combine multiple passes into one
- Complete one pass fully before moving to the next
- Maintain consistency across all changes
- Prefer clarity over cleverness
- Favor explicit structure over implicit coupling

---

# FINAL NOTE

This is a **boundary-hardening program**, not a feature expansion.

Success is defined by:
- clearer ownership
- smaller, more focused components
- reduced coupling
- improved maintainability

---

# REPORT REQUIREMENT (MANDATORY)

At the end of EACH pass, you MUST provide a structured report.

Failure to provide a report means the pass is incomplete.
