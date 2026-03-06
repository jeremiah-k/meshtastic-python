# Smoke1 Firmware Compatibility Matrix

## Scope

This matrix documents the expected Meshtastic CLI behavior used by
`meshtastic/tests/test_smoke1.py` so hardware smoke regressions are easy to
triage against firmware changes.

## Baseline

- Python CLI: `maint-35-1` (validated commit `68c95c9`)
- Firmware family: `2.7.x` (validated during this refactor cycle on `2.7.15`)
- Transport: single-node USB serial (`/dev/ttyACM*`)

## Stable Lane (`smoke1` without `smoke1_destructive`)

| Check                                                               | Expected behavior                                                             |
| ------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `--info`                                                            | Connects successfully and prints Owner/My info/Channels sections.             |
| invalid `--get` / `--set` / `--ch-set`                              | Returns zero exit code but prints field error text and `Choices are...` hint. |
| `--test` with one USB device                                        | Fails cleanly with two-device requirement message (`rc=1`).                   |
| `--debug`, `--seriallog`, `--qr`, `--nodes`, `--sendtext`, `--port` | Command succeeds without disconnect/close exceptions.                         |

## Destructive Lane (`smoke1_destructive`)

| Check                             | Expected behavior                                                                      |
| --------------------------------- | -------------------------------------------------------------------------------------- |
| Mutating command exit cleanliness | Successful mutating command must not emit `Bad file descriptor`/abort-on-close errors. |
| `--reboot`, `--factory-reset`     | Command succeeds and node reconnects after restart window.                             |
| `--pos-fields`                    | Uses modern symbolic fields (e.g., `ALTITUDE`, `ALTITUDE_MSL`, `DOP`).                 |
| `--ch-set modem_config ...`       | Reports unsupported channel field gracefully (`Choices are...`).                       |
| Channel CRUD/toggle flows         | Add/delete/enable/disable operations succeed across non-primary channels.              |
| `--seturl` invalid URL            | Fails with `There were no settings.` (`rc=1`).                                         |
| `--configure example_config.yaml` | Applies canonical snake_case keys and writes modified config successfully.             |

## Known Compatibility Notes

- `--configure` has historically varied across firmware builds; smoke assertions
  should validate user-visible behavior, not internal ordering/noise text.
- Some firmware exports can omit full channel arrays; integration tests should
  prefer strong identity signals (`channel_url`) with resilient fallbacks.
- Factory reset admin fields are integer-typed in protobuf; Python code must
  send integer values (`1`) rather than booleans.

## Update Procedure

When firmware behavior changes:

1. Capture hardware command output for changed behavior.
2. Update smoke assertions to match intentional behavior.
3. Update this matrix in the same PR with concrete notes.
4. Keep stable-lane checks non-destructive and move risky checks to
   `smoke1_destructive`.
