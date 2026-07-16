# Native SimRadio firmware testing

The native SimRadio suite complements the pinned Docker integration tests. It
launches installed `meshtasticd` binaries as disposable processes and bridges
their `SIMULATOR_APP` packets in Python. This lets daily, alpha, and beta
firmware packages exercise the current library without replacing the stable
2.7 container baseline.

## Running locally

Native tests currently require Linux and an executable `meshtasticd` binary.
The harness checks `MESHTASTICD_BIN` first and then `PATH`.

```bash
make simradio
```

The following environment variables are supported:

- `MESHTASTICD_BIN`: explicit executable path.
- `MESHTASTICD_SIM_BASE_PORT`: first TCP port for the three-node mesh; defaults
  to `4404`. Function-scoped single-node fixtures start at this value plus 100
  and consume a fresh sequential port for each test, avoiding immediate listener
  rebinding after reboot-heavy cases.
- `MESHTASTICD_SIM_LOG_DIR`: persistent artifact directory for per-process
  stdout and stderr. Without it, temporary logs are removed during teardown.

Do not run this marker concurrently in multiple pytest workers unless each
worker receives a distinct base port.

## Test isolation

Single-node CLI tests use a function-scoped, freshly erased simulator. The
three-node A-B-C chain is module-scoped because its tests do not mutate shared
configuration and all packet assertions use unique payloads and
interface-filtered subscriptions.

Every fixture:

1. starts owned process groups and temporary VFS directories;
2. sets the LoRa region to `US` for firmware 2.8 preset validation;
3. retries a real `TCPInterface` after reboot-capable writes instead of opening
   disposable readiness probes;
4. tears down subscriptions, interfaces, process groups, and files even after
   partial startup failure.

The single-node fixture releases its interface before yielding. CLI processes
therefore have exclusive ownership of the firmware API port, and state checks
open and close one temporary interface only after the CLI process exits. The
multi-node fixture retains one interface per distinct firmware port so its
packet bridge remains connected without client contention. Because that fixture
is module-scoped, text sends go through `SimMesh.send_text()`. Firmware 2.8
enforces a two-second `TEXT_MESSAGE_APP` PhoneAPI limit, so the helper applies a
small per-sender scheduling margin. This prevents test order and propagation
speed from deciding whether the next message is accepted.

Reboot-capable setup and reset tests wait for Portduino's next stable
`Using config file <port>` startup marker. A successful TCP reconnect by itself
is insufficient because the simulator can still accept clients during the
delayed pre-reboot window. The bridge also preserves optional decoded packet
metadata such as `bitfield`; firmware 2.8 requires that presence marker to
accept modern zero-hop packets.

CLI subprocess timeouts and firmware request timeouts are separate. `run_cli()`
therefore supplies a bounded `--timeout` value by default so optional admin reads
that a firmware build does not implement cannot outlive the subprocess budget.
Tests may override that request timeout explicitly, or pass `None` when they need
the CLI's production default.

## Failure artifacts

Firmware jobs archive both daemon logs and test-run diagnostics. Each channel
artifact includes the verbose pytest transcript, JUnit XML, installed Debian
package version, and a bounded `meshtasticd --version` probe. This keeps a
failed beta, alpha, or daily run diagnosable without relying on the Actions log
retention UI.

## CI policy

Pull-request CI runs beta, alpha, and daily PPA packages. Beta failures are
blocking (the PR cannot merge until they pass). Alpha and daily failures are
non-blocking so upstream regressions in pre-release channels are visible
without blocking unrelated changes. A scheduled workflow also runs the daily
package once per day and uploads all simulator logs regardless of outcome.

Markers:

- `simradio`: all process-managed native firmware tests.
- `simradio_mesh`: the multi-node topology subset.
- `smokevirt`: both native live-test modules, because they exercise virtual
  firmware devices.

The ordinary pytest selection excludes `simradio`; these tests run only when
explicitly selected or by their dedicated workflows.
