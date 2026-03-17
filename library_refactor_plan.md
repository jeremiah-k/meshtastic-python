# AGOR Architectural Refactor Program
## Coordinator Directive (High-Level Execution Plan)

## Execution Progress

- Program status: **PASS 1 In Progress (major ownership-transfer tranche landed)**
- Last updated: **2026-03-17 (ownership-transfer tranche: management + notifications)**

### Progress Log

#### PASS 1 — BLE Ownership Refactor (In Progress)

- Completed in current cycle:
  - Landed a large ownership-transfer tranche focused on management + notification domains:
    - Extracted notification safety/registration authority into `BLENotificationDispatcher`.
    - Rewired `_register_notifications`, `_from_num_handler`,
      `_report_notification_handler_error`, and `_invoke_safe_execute_compat`
      in `BLEInterface` to collaborator delegation.
    - Migrated malformed FROMNUM counter/lock and FROMNUM notify-enabled flags
      behind dispatcher-owned state with compatibility bridges on `BLEInterface`.
  - Refactored management helper ownership boundaries:
    - Added substantial management-domain helper implementations on
      `BLEManagementCommandHandler` (`resolve_target_address_for_management`,
      client selection/revalidation helpers, gate helper, operation accounting).
    - Converted interface-side management helpers to collaborator facades.
    - Bound a persistent management collaborator in `BLEInterface` with dynamic
      dependency lookup for monkeypatch-heavy compatibility paths.
  - Preserved compatibility contracts while moving authority:
    - `BLEInterface` keeps compatibility wrappers and module-level constants
      expected by existing tests/integrations.
    - Dynamic collaborator wiring ensures monkeypatched
      `meshtastic.interfaces.ble.interface.BLEClient` and
      `_is_currently_connected_elsewhere` paths still work.
  - Introduced a real instance-bound collaborator for BLE management commands (`BLEManagementCommandHandler`) in `management_service.py`.
  - Rewired `BLEInterface` management pathways to delegate through collaborator instances instead of static service calls for:
    - `_execute_management_command()`
    - timeout validators
    - `pair()`, `unpair()`, and `trust()`
    - `_run_bluetoothctl_trust_command()`
  - Preserved testability and behavioral compatibility by resolving collaborator dependencies dynamically at call time (important for monkeypatched `BLEClient` test paths).
  - Hardened notification safe-execute compatibility dispatch in `interface.py`:
    - widened positional signature-mismatch detection for legacy `TypeError` variants,
    - added explicit debug logging for compatibility-probe exceptions (including post-handler-execution probe failures),
    - retained no-duplicate-handler-execution behavior by keeping fallback execution conditional.
  - Applied compatibility inventory/documentation cleanup in `COMPATIBILITY.md`:
    - normalized `BLEStateManager.is_connecting()` method notation,
    - clarified that underscore-prefixed canonical symbols are implementation details,
    - marked `_lock` row as internal compatibility alias.
  - Consolidated BLE utils imports in `compatibility_service.py` for style consistency.
  - Completed runtime collaboratorization for remaining BLE orchestration services:
    - Added `BLELifecycleController` in `lifecycle_service.py`.
    - Added `BLEReceiveRecoveryController` in `receive_service.py`.
    - Added `BLECompatibilityEventPublisher` in `compatibility_service.py`.
  - Rewired `BLEInterface` runtime lifecycle/receive/compatibility paths to instance-bound collaborators instead of direct static service invocation.
  - Added lazy collaborator getters in `BLEInterface` for object-constructed and monkeypatch-heavy test compatibility.
  - Updated `BLE.md` architecture and boundary documentation to reflect the current collaborator-based runtime model.
  - Removed direct collaborator-private reach-through from `BLEInterface` for state/notification paths by introducing and using explicit collaborator APIs:
    - `NotificationManager.get_callback()/subscribe()/cleanup_all()/unsubscribe_all()/resubscribe_all()`
    - `BLEStateManager.current_state/is_connected/is_closing/can_connect/is_active`
  - Updated compatibility publisher collaborator construction to inject a publishing-thread provider, removing cross-module direct private method calls.
- Validation completed:
  - `ruff check meshtastic/interfaces/ble/interface.py meshtastic/interfaces/ble/management_service.py meshtastic/interfaces/ble/notifications.py`: passed.
  - `poetry run pytest -q tests/test_ble_interface_core.py tests/test_ble_utils_service_targets.py tests/test_ble_lifecycle_receive_targets.py tests/test_ble_connection_discovery_client_targets.py`: **264 passed**.
  - `poetry run pytest -q`: **1454 passed, 3 skipped, 92 deselected**.
  - `ruff check` on touched files: passed.
  - `poetry run pytest -q tests/test_ble_interface_core.py tests/test_ble_connection_discovery_client_targets.py tests/test_ble_lifecycle_receive_targets.py tests/test_ble_utils_service_targets.py`: **264 passed**.
  - `poetry run pytest -q tests/test_ble_interface_core.py tests/test_ble_lifecycle_receive_targets.py tests/test_ble_utils_service_targets.py tests/test_ble_integration_scenarios.py`: **256 passed**.
  - `poetry run pytest -q tests/test_ble_interface_core.py tests/test_ble_lifecycle_receive_targets.py tests/test_ble_utils_service_targets.py tests/test_ble_connection_discovery_client_targets.py`: **264 passed**.
  - `poetry run pytest -q`: **1454 passed, 3 skipped, 92 deselected**.
- PASS 1 completion notes:
  - Runtime BLE service calls now route through bound collaborators rather than static service classes.
  - Management/lifecycle/receive/compatibility orchestration responsibilities are now separated behind explicit collaborator objects.
  - Public behavior and compatibility pathways were preserved while shifting ownership boundaries.

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
