# Dependency Update Tracker (Phase: `dependencies-update-1`)

## Scope

This document is the high-level tracker for the follow-up dependency refresh
workstream described in [REFACTOR_PROGRAM.md](/home/jeremiah/dev/meshtastic/python/REFACTOR_PROGRAM.md#L380).

Goals:

1. Update dependencies toward current supported releases.
2. Clear active advisories from `poetry.lock`.
3. Record breakages/regressions caused by dependency updates.
4. Track mitigation decisions when an update cannot be taken immediately.

## Baseline Snapshot (2026-03-05)

Branch: `dependencies-update-1` (from `develop`).

### Baseline commands used

1. `poetry show --top-level`
2. `poetry show --outdated`
3. `TRUNK_INTERACTIVE=0 .trunk/trunk check --filter=osv-scanner --show-existing --no-progress poetry.lock`

### Baseline security findings (`poetry.lock`)

Active advisory findings before this phase:

1. `GHSA-68rp-wp8r-4726` (`flask` 3.0.3, low): patch target `>=3.1.3`.
2. `GHSA-hgf8-39gv-g3f2` (`werkzeug` 3.0.6, medium): patch target `>=3.1.4`.
3. `GHSA-87hc-h4r5-73f7` (`werkzeug` 3.0.6, medium): patch target `>=3.1.5`.
4. `GHSA-29vq-49wr-vm6x` (`werkzeug` 3.0.6, medium): patch target `>=3.1.6`.

## Upgrade Inventory (Outdated Packages)

| Package                   | Current           | Latest            | Delta | Area                         | Initial Risk | Status                                  |
| ------------------------- | ----------------- | ----------------- | ----- | ---------------------------- | ------------ | --------------------------------------- |
| astroid                   | 3.3.11            | 4.1.1             | major | lint toolchain (pylint dep)  | Medium       | blocked (pylint constraint)             |
| dash                      | 2.18.2            | 4.0.0             | major | analysis optional runtime    | High         | completed (Batch A)                     |
| dash-bootstrap-components | 1.7.1             | 2.0.4             | major | analysis optional runtime    | High         | completed (Batch A)                     |
| flask                     | 3.0.3             | 3.1.3             | minor | transitive (dash) + security | High         | completed (Batch A)                     |
| isort                     | 6.1.0             | 8.0.1             | major | lint toolchain               | Medium       | completed (Batch C)                     |
| mypy-protobuf             | 3.7.0             | 5.0.0             | major | typing codegen toolchain     | Medium       | completed (Batch C)                     |
| packaging                 | 24.2              | 26.0              | major | runtime utility              | Medium       | completed (Batch B)                     |
| pandas                    | 2.3.3             | 3.0.1             | major | analysis optional runtime    | High         | blocked (Python 3.10 support)           |
| pandas-stubs              | 2.3.3.260113      | 3.0.0.260204      | major | analysis typing              | Medium       | blocked (Python 3.10 support)           |
| pdoc3                     | 0.10.0            | 0.11.6            | minor | docs toolchain               | Low          | completed (Batch B)                     |
| plotly                    | 6.5.2             | 6.6.0             | minor | analysis optional runtime    | Low          | completed (Batch B via dash resolution) |
| pyinstaller-hooks-contrib | 2026.1            | 2026.2            | patch | build tooling                | Low          | completed (Batch B)                     |
| pylint                    | 3.3.9             | 4.0.5             | major | lint toolchain               | High         | completed (Batch C)                     |
| pytest                    | 8.4.2             | 9.0.2             | major | test toolchain               | High         | completed (Batch C)                     |
| pytest-cov                | 5.0.0             | 7.0.0             | major | test toolchain               | Medium       | completed (Batch C)                     |
| pytz                      | 2025.2            | 2026.1.post1      | patch | transitive                   | Low          | completed (Batch B)                     |
| tabulate                  | 0.9.0             | 0.10.0            | major | runtime output formatting    | Medium       | completed (Batch B)                     |
| types-pytz                | 2025.2.0.20251108 | 2026.1.1.20260304 | patch | typing                       | Low          | completed (Batch B)                     |
| wcwidth                   | 0.2.14            | 0.6.0             | major | optional CLI extra           | Medium       | completed (Batch B)                     |
| werkzeug                  | 3.0.6             | 3.1.6             | minor | transitive (dash) + security | High         | completed (Batch A)                     |

## Planned Execution Batches

### Batch A - Security and low-risk transitive remediation

Target: remove `flask`/`werkzeug` advisories with minimal behavioral churn.

Candidate approaches:

1. Update `dash` within compatible range if possible and observe transitive lift.
2. If needed, pin/raise transitive floor for `flask` + `werkzeug`.

Result:

- Completed by moving `dash` to `4.0.0` and `dash-bootstrap-components` to `2.0.4`.
- `dash 2.18.2` was a hard blocker for advisory remediation because it requires
  `flask <3.1` and `werkzeug <3.1`.
- Advisory findings for `flask`/`werkzeug` are now cleared from `poetry.lock`.

### Batch B - Non-breaking/low-risk tooling updates

Examples: `pdoc3`, `plotly`, `pyinstaller-hooks-contrib`, `pytz`, `types-pytz`.

Result:

- Completed.
- Updated:
  - `packaging 24.2 -> 26.0`
  - `pdoc3 0.10.0 -> 0.11.6`
  - `tabulate 0.9.0 -> 0.10.0`
  - `pyinstaller-hooks-contrib 2026.1 -> 2026.2`
  - `plotly 6.5.2 -> 6.6.0`
  - `pytz 2025.2 -> 2026.1.post1`
  - `types-pytz 2025.2.0.20251108 -> 2026.1.1.20260304`
  - `wcwidth 0.2.14 -> 0.6.0`

### Batch C - Test/lint/type major toolchain upgrades

Examples: `pytest`, `pytest-cov`, `pylint`, `astroid`, `isort`, `mypy-protobuf`.

Result:

- Completed for upgradable items.
- Updated:
  - `pytest 8.4.2 -> 9.0.2`
  - `pytest-cov 5.0.0 -> 7.0.0`
  - `pylint 3.3.9 -> 4.0.5`
  - `isort 6.1.0 -> 8.0.1`
  - `mypy-protobuf 3.7.0 -> 5.0.0`
  - `astroid 3.3.11 -> 4.0.4`
- Remaining `astroid` delta (`4.0.4 -> 4.1.1`) is blocked by current
  `pylint 4.0.5` dependency constraint (`astroid <=4.1.dev0`).

### Batch D - Analysis stack major upgrades

Examples: `dash`, `dash-bootstrap-components`, `pandas`, `pandas-stubs`.

Result:

- Partially completed.
- `dash`/`dash-bootstrap-components` upgraded in Batch A.
- `pandas 3.x` and `pandas-stubs 3.x` are blocked while project Python support
  remains `>=3.10`, because the `3.x` line requires Python `>=3.11`.

## Validation Protocol Per Batch

1. `poetry lock` (or targeted `poetry update ...`) and inspect lock diff.
2. `TRUNK_INTERACTIVE=0 .trunk/trunk check --fix --show-existing --no-progress`
3. Targeted checks first:
   - impacted unit/integration tests
   - `poetry run mypy meshtastic/`
   - `poetry run pylint meshtastic examples/ --ignore-patterns ".*_pb2\\.pyi?$"`
4. If the batch is broad enough, run `make ci-strict` once and then return to
   targeted runs only for subsequent fixes.

## Progress Log

### 2026-03-05 - Baseline collection

- Re-read `REFACTOR_PROGRAM.md` dependency follow-up section.
- Captured outdated package set and advisory baseline.
- Established execution batches and risk levels.
- No dependency versions changed yet in this log entry.

### 2026-03-05 - Batch A execution (security remediation)

- Updated dependency constraints:
  - `dash: ^2.17.1 -> ^4.0.0`
  - `dash-bootstrap-components: ^1.6.0 -> ^2.0.4`
- Resolved lock and synced environment:
  - `dash 4.0.0`
  - `dash-bootstrap-components 2.0.4`
  - `flask 3.1.3`
  - `werkzeug 3.1.6`
- Validation outcomes:
  - `make ci-strict` passed.
  - `poetry run pytest meshtastic/tests/test_analysis.py` passed.
  - `poetry run mypy meshtastic/analysis/__main__.py` passed.
  - `osv-scanner` for `poetry.lock` reports no findings.

### 2026-03-05 - Batch B execution (low-risk refresh)

- Updated `packaging`, `pdoc3`, `tabulate`, and `pyinstaller-hooks-contrib`.
- Additional transitive/extra updates landed during environment sync:
  - `plotly`, `pytz`, `types-pytz`, `wcwidth`.
- No code regressions observed from this batch.

### 2026-03-05 - Batch C execution (test/lint/type majors)

- Updated:
  - `pytest`, `pytest-cov`, `pylint`, `isort`, `mypy-protobuf`, `astroid`.
- Full gate run (`make ci-strict`) surfaced one strict-mypy regression:
  - `meshtastic/analysis/__main__.py`: unused `type: ignore`.
- Fix applied by removing stale `type: ignore` on `from dash import ...`.
- Revalidated strict mypy:
  - `poetry run mypy meshtastic/ --strict` passed.

### 2026-03-05 - Batch D investigation (pandas 3 line)

- Attempted upgrade to `pandas 3.0.1` and `pandas-stubs 3.0.0.260204`.
- Resolver failure confirmed:
  - `pandas 3.x` requires Python `>=3.11`.
  - project currently supports Python `>=3.10,<3.15`.
- Decision: keep current pandas 2.x line and record as blocked pending policy
  change to drop Python 3.10 support.

### 2026-03-05 - Policy confirmation

- Confirmed project policy for this phase: keep minimum supported Python at 3.10.
- Consequence:
  - keep `pandas` and `pandas-stubs` on 2.x-compatible constraints,
  - do not force Python-marker split that changes effective support policy.
