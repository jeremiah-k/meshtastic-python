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
  to `4404`. Single-node fixtures use this value plus 100.
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
is module-scoped, text sends go through `SimMesh.send_text()`, which enforces a
small per-sender interval. Current alpha and daily firmware reject a second
`TEXT_MESSAGE_APP` packet sent too soon after the first instead of queueing it;
the harness-level pacing prevents test order and propagation speed from deciding
whether a message is accepted.

CLI subprocess timeouts and firmware request timeouts are separate. `run_cli()`
therefore supplies a bounded `--timeout` value by default so optional admin reads
that a firmware build does not implement cannot outlive the subprocess budget.
Tests may override that request timeout explicitly, or pass `None` when they need
the CLI's production default.

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
