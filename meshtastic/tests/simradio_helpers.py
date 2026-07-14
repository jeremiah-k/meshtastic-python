"""CLI, connection, and packet helpers for native simradio smoke tests."""

from __future__ import annotations

import contextlib
import logging
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pubsub import pub

from meshtastic.tcp_interface import TCPInterface

logger = logging.getLogger(__name__)

PAUSE_AFTER_CLI_SECONDS = 0.2
PAUSE_AFTER_REGION_CHANGE_SECONDS = 2.0
DEFAULT_CLI_TIMEOUT_SECONDS = 60.0
DEFAULT_CONNECT_WAIT_SECONDS = 30.0
DEFAULT_RECEIVE_TIMEOUT_SECONDS = 15.0
DEFAULT_ABSENCE_TIMEOUT_SECONDS = 3.0
CLI_TIMEOUT_RETURN_CODE = 124
_TRANSIENT_CLI_OUTPUT = (
    "error connecting",
    "timed out waiting for connection",
    "connection reset by peer",
    "connection refused",
)


def _redact_cli_diagnostics(output: str, arguments: Sequence[str]) -> str:
    """Remove positional CLI values before including output in exceptions."""
    redacted = output
    for argument in arguments:
        if argument and not argument.startswith("--"):
            redacted = redacted.replace(argument, "<redacted>")
    return redacted


@dataclass(frozen=True)
class CLIResult:
    """Result of one possibly retried in-tree CLI invocation."""

    returncode: int
    output: str
    attempts: int
    timed_out: bool = False


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    """Normalize TimeoutExpired output across text and bytes subprocess modes."""
    output = exc.stdout or exc.stderr or ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


# ---------------------------------------------------------------------------
# Operation-aware retry policy
# ---------------------------------------------------------------------------

# Destructive operations that must never be retried.
_DESTRUCTIVE_ARGUMENTS: frozenset[str] = frozenset(
    {
        "--ch-add",
        "--ch-del",
        "--ch-enable",
        "--ch-disable",
        "--factory-reset",
        "--factory-reset-config",
        "--factory-reset-device",
        "--reboot",
        "--reboot-ota",
        "--enter-dfu",
        "--shutdown",
        "--ota-update",
        "--reset-nodedb",
        "--test",  # sends stress-test traffic
    }
)

# Operations that never mutate device state.
_READ_ONLY_ARGUMENTS: frozenset[str] = frozenset(
    {
        "--info",
        "--nodes",
        "--qr",
        "--get",
        "--export-config",
        "--list-fields",
        "--support",
        "--device-metadata",
    }
)

# Operations that are safe to retry because repeating them produces the
# same result (set owner, configure a channel, etc.).
_IDEMPOTENT_ARGUMENTS: frozenset[str] = frozenset(
    {
        "--set",
        "--set-owner",
        "--set-owner-short",
        "--set-ham",
        "--set-position",
        "--seturl",
    }
)

# Semantic --set values that trigger destructive device actions.
_DESTRUCTIVE_SET_VALUES: frozenset[str] = frozenset(
    {
        "factory_reset",
        "reboot",
        "shutdown",
        "ota_update",
    }
)

# Default retries per operation kind.  Only read-only and explicitly safe
# idempotent mutations are allowed to retry.  Everything else defaults to
# zero so an ambiguous transport failure cannot duplicate side effects.
_DEFAULT_RETRIES: dict[str, int] = {
    "read_only": 2,
    "idempotent_mutation": 2,
    "non_idempotent": 0,
}


def _classify_cli_operation(arguments: Sequence[str]) -> str:
    """Map CLI arguments to ``read_only``, ``idempotent_mutation``, or ``non_idempotent``.

    Destructive arguments take priority, then semantic ``--set destructive``
    forms, then read-only allowlist, then idempotent-mutation allowlist.
    Unknown operations default to ``non_idempotent``.
    """
    # Phase 1: explicit destructive flags
    for argument in arguments:
        if argument in _DESTRUCTIVE_ARGUMENTS:
            return "non_idempotent"

    # Phase 2: semantic --set with destructive field values
    for i, argument in enumerate(arguments):
        if argument == "--set" and i + 1 < len(arguments):
            field = arguments[i + 1].split(".")[0]
            if field in _DESTRUCTIVE_SET_VALUES:
                return "non_idempotent"

    # Phase 3: explicit read-only flags
    for argument in arguments:
        if argument in _READ_ONLY_ARGUMENTS:
            return "read_only"

    # Phase 4: explicit idempotent-mutation flags
    for argument in arguments:
        if argument in _IDEMPOTENT_ARGUMENTS:
            return "idempotent_mutation"

    # Phase 5: default conservative — assume non-idempotent
    return "non_idempotent"


def run_cli(
    port: int,
    *arguments: str,
    timeout: float = DEFAULT_CLI_TIMEOUT_SECONDS,
    retries: int | None = None,
    retry_delay: float = 1.0,
) -> CLIResult:
    """Run the in-tree CLI against one simulator with transient retries.

    ``retries`` controls the maximum retry count after the first attempt.
    When ``retries`` is ``None`` (the default), the value is selected from
    :data:`_DEFAULT_RETRIES` based on the detected operation kind.
    """
    if retries is None:
        retries = _DEFAULT_RETRIES[_classify_cli_operation(arguments)]
    if retries < 0:
        raise ValueError("retries must not be negative")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if retry_delay < 0:
        raise ValueError("retry_delay must not be negative")
    argv = [
        sys.executable,
        "-m",
        "meshtastic",
        "--host",
        f"localhost:{port}",
        *arguments,
    ]
    # Never log positional values: configuration commands may contain Wi-Fi
    # credentials, private keys, or lockdown passphrases. Option names and the
    # target port provide enough diagnostics without retaining secrets.
    logger.debug(
        "Running simradio CLI on port %d with options %s",
        port,
        [argument for argument in arguments if argument.startswith("--")],
    )
    last_output = ""
    for attempt in range(1, retries + 2):
        try:
            completed = subprocess.run(  # noqa: S603
                argv,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            last_output = _timeout_output(exc)
            if attempt <= retries:
                time.sleep(retry_delay)
                continue
            return CLIResult(
                CLI_TIMEOUT_RETURN_CODE,
                last_output,
                attempt,
                timed_out=True,
            )

        last_output = completed.stdout
        transient_failure = completed.returncode != 0 and any(
            marker in last_output.casefold() for marker in _TRANSIENT_CLI_OUTPUT
        )
        if transient_failure and attempt <= retries:
            logger.debug(
                "Retrying transient simradio CLI failure (%d/%d)",
                attempt,
                retries + 1,
            )
            time.sleep(retry_delay)
            continue
        return CLIResult(completed.returncode, last_output, attempt)
    raise AssertionError("simradio CLI retry loop exhausted unexpectedly")


def _wait_for_port(
    port: int,
    *,
    timeout: float = DEFAULT_CONNECT_WAIT_SECONDS,
) -> None:
    """Wait for a simulator TCP listener after a reboot-capable write."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"localhost:{port} did not accept connections in {timeout:.1f}s")


def connect_iface(
    port: int,
    *,
    no_nodes: bool = False,
    retries: int = 4,
    wait_timeout: float = DEFAULT_CONNECT_WAIT_SECONDS,
) -> TCPInterface:
    """Open a fresh configured TCPInterface with reboot-aware retries."""
    if retries < 0:
        raise ValueError("retries must not be negative")
    last_exception: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            _wait_for_port(port, timeout=wait_timeout)
            return TCPInterface(
                hostname="localhost",
                portNumber=port,
                connectNow=True,
                connectTimeout=10.0,
                noNodes=no_nodes,
            )
        except Exception as exc:  # pylint: disable=broad-except
            last_exception = exc
            if attempt <= retries:
                logger.debug(
                    "Retrying simradio interface connection (%d/%d): %s",
                    attempt,
                    retries + 1,
                    exc,
                )
                time.sleep(0.5)
                continue
            raise
    raise AssertionError(f"unreachable connection loop: {last_exception}")


def verify_state(
    port: int,
    verifier: Callable[[TCPInterface], None],
    *,
    no_nodes: bool = False,
) -> None:
    """Verify firmware state through a fresh library connection."""
    iface = connect_iface(port, no_nodes=no_nodes)
    try:
        verifier(iface)
    finally:
        with contextlib.suppress(Exception):
            iface.close()
        time.sleep(PAUSE_AFTER_CLI_SECONDS)


def cli_then_verify(
    port: int,
    arguments: Sequence[str],
    verifier: Callable[[TCPInterface], None] | None,
    *,
    expected_returncode: int | None = 0,
    no_nodes: bool = False,
    cli_timeout: float = DEFAULT_CLI_TIMEOUT_SECONDS,
) -> CLIResult:
    """Run a CLI action, assert its result, then verify state independently."""
    result = run_cli(port, *arguments, timeout=cli_timeout)
    if expected_returncode is not None and result.returncode != expected_returncode:
        option_names = [
            argument for argument in arguments if argument.startswith("--")
        ]
        raise AssertionError(
            f"CLI returned {result.returncode}; expected {expected_returncode}.\n"
            f"Command options: {option_names!r}\n"
            f"{_redact_cli_diagnostics(result.output, arguments)}"
        )
    time.sleep(PAUSE_AFTER_CLI_SECONDS)
    if verifier is not None:
        verify_state(port, verifier, no_nodes=no_nodes)
    return result


def set_region(port: int, region: str = "US") -> None:
    """Set the simulator LoRa region and wait for its listener to settle."""
    result = run_cli(port, "--set", "lora.region", region)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to set lora.region={region} on port {port}:\n{result.output}"
        )
    time.sleep(PAUSE_AFTER_REGION_CHANGE_SECONDS)
    _wait_for_port(port)


class PacketCollector:
    """Thread-safe, interface-filtered pubsub packet subscription."""

    def __init__(self, iface: TCPInterface, topic: str) -> None:
        self.iface = iface
        self.topic = topic
        self._condition = threading.Condition()
        self._packets: list[dict[str, Any]] = []
        self._closed = False

        def _handler(packet: dict[str, Any], interface: TCPInterface) -> None:
            if interface is not self.iface:
                return
            with self._condition:
                if self._closed:
                    return
                self._packets.append(packet)
                self._condition.notify_all()

        self._handler = _handler
        pub.subscribe(self._handler, topic)

    @property
    def packets(self) -> list[dict[str, Any]]:
        """Return a stable snapshot of collected packets."""
        with self._condition:
            return list(self._packets)

    @property
    def texts(self) -> list[str]:
        """Return decoded text payloads from the current packet snapshot."""
        return [
            str(decoded.get("text", ""))
            for packet in self.packets
            if isinstance((decoded := packet.get("decoded")), dict)
            and decoded.get("portnum") == "TEXT_MESSAGE_APP"
        ]

    @property
    def traceroutes(self) -> list[dict[str, Any]]:
        """Return decoded traceroute packets from the current snapshot."""
        return [
            packet
            for packet in self.packets
            if isinstance((decoded := packet.get("decoded")), dict)
            and decoded.get("portnum") == "TRACEROUTE_APP"
        ]

    @property
    def telemetries(self) -> list[dict[str, Any]]:
        """Return decoded telemetry packets from the current snapshot."""
        return [
            packet
            for packet in self.packets
            if isinstance((decoded := packet.get("decoded")), dict)
            and decoded.get("portnum") == "TELEMETRY_APP"
        ]

    @property
    def positions(self) -> list[dict[str, Any]]:
        """Return decoded position packets from the current snapshot."""
        return [
            packet
            for packet in self.packets
            if isinstance((decoded := packet.get("decoded")), dict)
            and decoded.get("portnum") == "POSITION_APP"
        ]

    def wait_for_packet(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = DEFAULT_RECEIVE_TIMEOUT_SECONDS,
    ) -> dict[str, Any] | None:
        """Wait until a packet matches ``predicate`` and return that packet."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for packet in self._packets:
                    if predicate(packet):
                        return packet
                if self._closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)

    def wait_for_text(
        self,
        text: str,
        *,
        timeout: float = DEFAULT_RECEIVE_TIMEOUT_SECONDS,
    ) -> bool:
        """Wait for an exact decoded text message."""
        return (
            self.wait_for_packet(
                lambda packet: (
                    isinstance(packet.get("decoded"), dict)
                    and packet["decoded"].get("portnum") == "TEXT_MESSAGE_APP"
                    and packet["decoded"].get("text") == text
                ),
                timeout=timeout,
            )
            is not None
        )

    def assert_no_text(
        self,
        text: str,
        *,
        timeout: float = DEFAULT_ABSENCE_TIMEOUT_SECONDS,
    ) -> None:
        """Assert that an exact text does not arrive during a bounded window."""
        packet = self.wait_for_packet(
            lambda candidate: (
                isinstance(candidate.get("decoded"), dict)
                and candidate["decoded"].get("portnum") == "TEXT_MESSAGE_APP"
                and candidate["decoded"].get("text") == text
            ),
            timeout=timeout,
        )
        if packet is not None:
            raise AssertionError(f"Unexpected text {text!r} received: {packet!r}")

    def clear(self) -> None:
        """Discard collected packets while keeping the subscription active."""
        with self._condition:
            self._packets.clear()

    def close(self) -> None:
        """Unsubscribe only this collector without disturbing other listeners."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        with contextlib.suppress(Exception):
            pub.unsubscribe(self._handler, self.topic)

    def __enter__(self) -> PacketCollector:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def subscribe_texts(iface: TCPInterface) -> PacketCollector:
    """Collect text packets received by one interface."""
    return PacketCollector(iface, "meshtastic.receive.text")


def subscribe_traceroutes(iface: TCPInterface) -> PacketCollector:
    """Collect traceroute packets received by one interface."""
    return PacketCollector(iface, "meshtastic.receive.traceroute")


def subscribe_telemetries(iface: TCPInterface) -> PacketCollector:
    """Collect telemetry packets received by one interface."""
    return PacketCollector(iface, "meshtastic.receive.telemetry")


def subscribe_positions(iface: TCPInterface) -> PacketCollector:
    """Collect position packets received by one interface."""
    return PacketCollector(iface, "meshtastic.receive.position")
