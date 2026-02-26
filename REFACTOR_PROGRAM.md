# Meshtastic Python Refactor Program

## 1) Purpose

This document explains the full refactor program for the Meshtastic Python
library, including why scope expanded beyond BLE, what policy decisions were
made, and how backward compatibility was preserved while improving correctness.

This is an engineering rationale document, not an announcement.

## 2) Background and Scope Expansion

The effort started as a BLE-only stabilization and architecture rewrite.
During implementation and review, repeated BLE rewrites exposed broader
codebase issues:

- Failing tests outside BLE.
- Large linter/type-check error volume.
- Inconsistent naming and typing patterns across modules.
- Hidden race conditions and lockless shared-state access.

At that point, limiting work to BLE alone would have left the library in a
partially-modernized state where new BLE code depended on old patterns in the
rest of the package. Scope was intentionally expanded to align the overall
library with the same engineering standards.

The BLE package itself went through multiple full iterations before converging
on stable architecture and behavior.

## 3) Program Goals

- Stabilize BLE connection lifecycle and reconnect behavior.
- Improve correctness under concurrency and callback-driven execution.
- Move to Python 3.10+ typing and syntax consistently.
- Normalize naming conventions:
  - camelCase for public API.
  - underscore-prefixed snake_case for internal helpers.
  - explicit compatibility shims where required.
- Preserve historical public API behavior where compatibility is required.
- Tighten test coverage where refactors exposed latent bugs.
- Keep changes reviewable with explicit policy, markers, and regression tests.

## 4) Non-Goals

- No Python 3.9 compatibility work.
- No broad API removals from historical public surfaces without explicit policy.
- No large behavior changes in user-facing paths unless justified by safety or
  correctness and backed by tests.

## 5) Source-of-Truth Compatibility Baseline (Pinned)

To avoid a moving compatibility target, this refactor uses one pinned baseline
for the entire API surface in this PR:

- Tag: `2.7.7`
- Commit: `b26d80f1866ffa765467e5cb7688c59dee7f2bb2`

All compatibility decisions in this PR (BLE and non-BLE) are evaluated against
that same baseline.

Required historical BLE methods remain callable:

- `BLEClient.async_await`
- `BLEClient.async_run`
- `BLEInterface.from_num_handler`
- `BLEInterface.log_radio_handler`
- `BLEInterface.legacy_log_radio_handler`

These are implemented as stable compatibility shims over canonical internal
helpers and are intentionally silent (no deprecation warnings).

Examples where compatibility was explicitly preserved or restored:

- `meshtastic.util.message_to_json` restored as a stable snake_case alias for
  `messageToJson`.
- Existing snake_case wrappers retained where historical callers rely on them.

## 6) Standards Adopted Across the Refactor

## 6.1 Typing and Python version

- Target runtime: Python 3.10+.
- PEP 604 union syntax (`X | None`) and modern annotations used consistently.
- Type hints expanded in core paths, tests, and callback signatures.
- Removed stale compatibility patterns that only existed for older runtimes.

## 6.2 Naming policy

- Public API methods: camelCase.
- Internal helpers: underscore-prefixed snake_case.
- Historical names remain callable via shims when they are part of maintained
  compatibility surface.

Detailed naming policy is tracked in `AGENTS.md`.

## 6.3 Compatibility markers

Compatibility wrappers are tagged with grep-friendly comments:

- `COMPAT_STABLE_SHIM`
- `COMPAT_DEPRECATE`

This allows one-pass inventorying and future cleanup planning.

## 6.4 Deprecation discipline

- Naming-only compatibility wrappers are generally silent unless explicit
  deprecation policy exists.
- Semantic migrations may emit warnings.
- Warn-once behavior is used where deprecated code could be called repeatedly
  (to avoid warning spam and runtime overhead).

## 7) Major Technical Workstreams

## 7.1 BLE architecture rewrite and stabilization

BLE moved from monolithic logic to focused modules for:

- state/lifecycle management,
- discovery,
- connection orchestration,
- notification management,
- retry/reconnect policy,
- error handling.

Key properties achieved:

- Explicit lock ordering and lifecycle ownership.
- Serialized reconnect handling.
- Safer disconnect paths.
- Better cancellation and timeout mapping in async wrappers.
- Public BLE export boundary kept narrow and explicit.

The dedicated BLE document (`BLE.md`) captures lower-level BLE internals.

## 7.2 Cross-codebase type and API normalization

Applied the same standards outside BLE:

- Explicit type hints in runtime code and tests.
- Consistent naming and aliasing patterns in powermon/slog/util/mt_config.
- Reduced mixed-style APIs where no compatibility requirement existed.

## 7.3 Concurrency and race-condition hardening

Refactor work and new tests exposed thread-safety gaps in shared-state paths.
High-impact fixes were applied in `mesh_interface.py`:

- Added lock protection for packet ID generation.
- Added lock protection for queue/queueStatus operations.
- Added lock protection for node database access paths.
- Removed response-handler TOCTOU window by making lookup/eligibility/pop atomic.

Additional race-hardening was applied in transport and BLE paths where reconnect
and shutdown can overlap.

## 7.4 Correctness and guard improvements

Examples of guard hardening added during this cycle:

- Improved invalid input checks in several interfaces.
- Protected callback paths from inconsistent state transitions.
- Added safer cleanup semantics where failures previously leaked state.

## 7.5 Test suite expansion and tightening

Tests were expanded and modernized to cover:

- compatibility aliases,
- deprecation behavior,
- concurrency-sensitive control paths,
- reconnect and cleanup edge cases,
- regression cases found during refactor.

Special focus area: `--export-config` / `--configure` round-trip correctness.
This path had prior instability and now has dedicated coverage in both unit and
smoke-virtual tests, including:

- exported security key encoding/decoding invariants,
- mixed snake_case/camelCase configuration key handling,
- full export->configure->export round-trip parity,
- device-facing configure flows against the virtual target.

The goal was to convert review findings into executable regression coverage,
not only local code fixes.

## 8) Behavior Changes: Intent, Rationale, and Compatibility Trade-offs

Some behavior changes were intentionally accepted because they improve library
quality and embedding safety.

## 8.1 Process exits to exceptions

Direction:

- Replace process-terminating flows in library internals with typed exceptions.

Why:

- Libraries should report failures to callers, not terminate host processes.
- Enables safe embedding in services, tests, and automation.
- Allows caller-controlled retry/backoff/recovery strategies.

Impact:

- Callers that relied on process exit must now handle exceptions explicitly.

## 8.2 `print` to structured logging in library internals

Direction:

- Prefer module logger for internal status/error reporting.

Why:

- Better control for library consumers (levels, handlers, filtering).
- Avoids forcing stdout side effects in embedded contexts.
- Improves testability and operational observability.

Impact:

- Consumers scraping stdout for internal events should migrate to log handling.

## 8.3 Sensitive payload handling

Direction:

- Keep live packet processing behavior compatible.
- Redact sensitive admin payload body only in cached node state snapshots.

Why:

- Reduce accidental retention of sensitive admin/session material.
- Preserve runtime packet semantics for handlers/callbacks.

## 8.4 Localhost-by-default web binding

Direction:

- Default analysis web-server bind changed from `0.0.0.0` to `127.0.0.1`.

Why:

- Safer default for local development and diagnostics.
- Avoids unintentionally exposing analysis endpoints on all interfaces.
- Still preserves explicit remote-use behavior via `--bind-host 0.0.0.0`.

Impact:

- Existing workflows expecting remote-by-default must now pass
  `--bind-host 0.0.0.0` (or another explicit interface address).

## 9) Backward Compatibility Strategy by Category

## 9.1 Stable compatibility (no warnings)

Used for historical public APIs that should remain callable and predictable.

Examples:

- Historical BLE public methods from 2.7.7 baseline.
- Stable snake_case wrappers required by existing callers.

## 9.2 Deprecated compatibility (warn-once where applicable)

Used for legacy spellings or transitional APIs with a clear migration path.

Examples:

- Legacy unit-suffix typo method variants in powermon.
- Selected module-level legacy aliases in utility/config modules.

## 9.3 Semantic migration warnings

Used where caller input changes behavior in a meaningful way and warning is
important to prevent silent misuse.

## 10) Quality Gates and Validation Process

Work was continuously validated with project gates:

- Linting and formatting via trunk:
  - `.trunk/trunk check --fix --show-existing`
- CI parity checks:
  - `make ci`
  - `pylint meshtastic examples/ --ignore-patterns ".*_pb2.pyi?$"`
  - `mypy meshtastic/`
  - `pytest --cov=meshtastic --cov-report=xml`

During iterative review cycles, targeted tests were run for touched areas to
reduce cycle time while keeping confidence high.

Security signal from trunk was also monitored with `osv-scanner`.

## 11) Documentation and Policy Artifacts

Two key documents now serve different levels:

- `BLE.md`: BLE-specific architecture and integration details.
- `AGENTS.md`: coding/refactor policy, naming/typing conventions, and
  compatibility marker discipline.

This document (`REFACTOR_PROGRAM.md`) is the high-level program rationale and
cross-cutting decision record.

## 12) What This Enables

This refactor positions the library for:

- safer long-running use in services and automation,
- more predictable API evolution,
- lower regression risk through explicit compatibility and tests,
- easier contributor onboarding through clear conventions and markers.

## 13) Contributor Guidance for Follow-up Changes

When touching APIs after this refactor:

- Confirm whether the symbol is historical public surface.
- Use `COMPAT_STABLE_SHIM` or `COMPAT_DEPRECATE` intentionally.
- Prefer warn-once for deprecated hot-path aliases.
- Add or update regression tests when changing compatibility behavior.
- Avoid broad behavior changes unless clearly justified and documented.

Useful inventory command:

- `rg -n "COMPAT_STABLE_SHIM|COMPAT_DEPRECATE" meshtastic`

## 14) Scope Summary

The program began as BLE stabilization and intentionally expanded to complete
the modernization work across the library once systemic issues were visible.
The outcome is not a BLE-only rewrite but a coordinated codebase quality pass:

- architecture cleanup,
- typing and naming consistency,
- concurrency safety,
- compatibility preservation,
- stronger tests and guardrails.

That expanded scope is the reason the resulting PR is larger than a subsystem
refactor, and also the reason it can be maintained coherently after merge.

## 15) Planned Follow-Up: Dependency Refresh and Security Updates

This refactor intentionally focused first on architecture, correctness,
compatibility, and test stability. A follow-up PR is planned to update the rest
of the dependency set and handle any resulting regressions.

Current security advisories reported by:

- `.trunk/trunk check --filter=osv-scanner --show-existing`

At the time of this document update, `poetry.lock` reports:

- `flask 3.0.3`: `GHSA-68rp-wp8r-4726` (low)
  - Patch guidance: upgrade to `>=3.1.3`
- `werkzeug 3.0.6`: `GHSA-29vq-49wr-vm6x` (medium)
  - Patch guidance: upgrade to `>=3.1.6`
- `werkzeug 3.0.6`: `GHSA-87hc-h4r5-73f7` (medium)
  - Patch guidance: upgrade to `>=3.1.5`
- `werkzeug 3.0.6`: `GHSA-hgf8-39gv-g3f2` (medium)
  - Patch guidance: upgrade to `>=3.1.4`

The follow-up dependency PR will:

- align packages to current supported releases,
- clear the above advisories,
- run full lint/type/test gates,
- and include regression fixes needed by dependency behavior changes.
