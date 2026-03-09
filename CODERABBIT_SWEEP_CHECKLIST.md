# CodeRabbit Sweep Checklist

This file tracks branch-local review triage and execution details that are
intentionally time-bound.

## Scope

- Track actionable review findings across touched files.
- Record disposition for each finding (`fixed`, `not-applicable`, `deferred`).
- Capture verification commands and outcomes.

## Execution Steps

1. Group duplicate comments by file/behavior to avoid conflicting edits.
2. Apply functional fixes before nits and naming cleanup.
3. Add or update regression tests for behavior changes.
4. Run targeted suites for touched areas.
5. Run `make ci-strict` once before final handoff.

## Verification Log (Template)

- Date:
- Branch:
- Commands:
  - `poetry run pytest <targeted tests>`
  - `make ci-strict`
- Result:
  - `pass` / `fail`
- Notes:
  - Any deferred follow-ups or CI-only checks.
