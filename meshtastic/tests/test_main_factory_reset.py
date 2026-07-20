"""Meshtastic unit tests for __main__.py."""

# pylint: disable=C0302,W0613,R0917

import logging
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

import meshtastic.__main__ as main_module
from meshtastic import mt_config
from meshtastic.__main__ import (
    main,
)

# from ..radioconfig_pb2 import UserPreferences
# import meshtastic.config_pb2
from ..serial_interface import SerialInterface
from ..util import Acknowledgment

# from ..ble_interface import BLEInterface


# from ..remote_hardware import onGPIOreceive
# from ..config_pb2 import Config

SDS_DISABLED_SENTINEL: int = 4_294_967_295
MAIN_LOCAL_ADDR: str = cast(str, main_module.__dict__["LOCAL_ADDR"])


def _get_config_field(config: Any, dotted_path: str) -> Any:
    """Walk a dotted `section.field` path on a protobuf Config message."""
    obj = config
    for part in dotted_path.split("."):
        obj = getattr(obj, part)
    return obj


@pytest.fixture(autouse=True)
def _mock_newer_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent external network calls during unit tests in this module.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Pytest monkeypatching fixture.
    """
    monkeypatch.setattr("meshtastic.util.check_if_newer_version", lambda: None)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("reset_flag", "expected_full"),
    [("--factory-reset", False), ("--factory-reset-device", True)],
)
def test_main_factory_reset_local_accepts_ack_before_close(
    capsys: pytest.CaptureFixture[str],
    reset_flag: str,
    expected_full: bool,
) -> None:
    """Both local reset variants should accept their correlated ACK before closing."""
    sys.argv = ["", reset_flag]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface._acknowledgment = Acknowledgment()
    iface._acknowledgment.receivedAck = True  # stale state must be cleared
    iface._timeout.expireTimeout = 1.0
    iface._wait_for_request_ack.return_value = True
    iface._raise_wait_error_if_present.return_value = None
    reset_node = iface.getNode.return_value
    reset_node.iface = iface

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is expected_full
        assert iface._acknowledgment.receivedAck is False
        iface._acknowledgment.receivedImplAck = True
        return SimpleNamespace(id=123)

    reset_node.factoryReset.side_effect = _factory_reset

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    out, err = capsys.readouterr()
    reset_node.factoryReset.assert_called_once_with(full=expected_full)
    iface._wait_for_request_ack.assert_called_once()
    wait_args, wait_kwargs = iface._wait_for_request_ack.call_args
    assert wait_args[:2] == ("receivedNak", 123)
    assert (
        0
        < wait_kwargs["timeout_seconds"]
        <= (main_module.FACTORY_RESET_ACCEPTANCE_POLL_SECONDS)
    )
    iface._raise_wait_error_if_present.assert_called_once_with(
        "receivedNak", request_id=123
    )
    iface._retire_wait_request.assert_called_once_with("receivedNak", request_id=123)
    iface.waitForAckNak.assert_not_called()
    assert iface._acknowledgment.receivedImplAck is False
    assert "Waiting for factory reset acknowledgment or reboot disconnect" in out
    assert err == ""


@pytest.mark.unit
def test_local_factory_reset_accepts_transient_reboot_disconnect() -> None:
    """A 2.7-style dropped ACK is accepted only after reboot disconnect is observed."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=Acknowledgment(),
        _timeout=SimpleNamespace(expireTimeout=1.0),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(iface=iface)

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is False
        request = SimpleNamespace(id=456)

        def _disconnect_after_send() -> None:
            time.sleep(0.01)
            # Model a fast reconnect: the pubsub edge must remain observable even
            # though isConnected is already set again by the time the wait polls.
            main_module.pub.sendMessage("meshtastic.connection.lost", interface=iface)
            connected.set()

        threading.Thread(target=_disconnect_after_send, daemon=True).start()
        return request

    reset_node.factoryReset = MagicMock(side_effect=_factory_reset)

    request = main_module._send_local_factory_reset_and_wait(
        reset_node,
        full=False,
        timeout=0.5,
    )

    assert request is not None
    assert request.id == 456
    iface._retire_wait_request.assert_called_once_with("receivedNak", request_id=456)


@pytest.mark.unit
def test_local_factory_reset_accepts_implicit_ack_with_legacy_completion_flag() -> None:
    """A valid implicit ACK must win over the legacy receivedNak completion latch."""
    connected = threading.Event()
    connected.set()
    acknowledgment = Acknowledgment()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=acknowledgment,
        _retire_wait_request=MagicMock(),
        _raise_wait_error_if_present=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is False
        acknowledgment.receivedImplAck = True
        acknowledgment.receivedNak = True
        return SimpleNamespace(id=457)

    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(side_effect=_factory_reset),
    )

    request = main_module._send_local_factory_reset_and_wait(
        reset_node,
        full=False,
        timeout=0.5,
    )

    assert request is not None
    assert request.id == 457
    iface._raise_wait_error_if_present.assert_called()


@pytest.mark.unit
def test_local_factory_reset_accepts_tcp_socket_generation_change() -> None:
    """A TCP reconnect is observable even when isConnected never clears."""
    connected = threading.Event()
    connected.set()
    original_socket = object()
    iface = SimpleNamespace(
        isConnected=connected,
        socket=original_socket,
        stream=None,
        _acknowledgment=Acknowledgment(),
        _timeout=SimpleNamespace(expireTimeout=0.01),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(iface=iface)

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is False

        def _replace_socket() -> None:
            time.sleep(0.05)
            iface.socket = object()

        threading.Thread(target=_replace_socket, daemon=True).start()
        return SimpleNamespace(id=458)

    reset_node.factoryReset = MagicMock(side_effect=_factory_reset)

    started = time.monotonic()
    request = main_module._send_local_factory_reset_and_wait(
        reset_node,
        full=False,
    )

    assert request is not None
    assert request.id == 458
    assert time.monotonic() - started >= 0.04
    # The interface's ordinary 10 ms timeout must not truncate the reset's
    # firmware-defined seven-second reboot window.
    assert iface._timeout.expireTimeout == 0.01


@pytest.mark.unit
def test_local_factory_reset_ignores_socket_change_before_send_returns() -> None:
    """A transport change before a concrete request returns is not acceptance."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        socket=object(),
        stream=None,
        _acknowledgment=Acknowledgment(),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is False
        iface.socket = object()
        return SimpleNamespace(id=459)

    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(side_effect=_factory_reset),
    )

    with pytest.raises(
        RuntimeError,
        match="Timed out waiting for a factory reset acknowledgment or reboot disconnect",
    ):
        main_module._send_local_factory_reset_and_wait(
            reset_node,
            full=False,
            timeout=0.01,
        )


@pytest.mark.unit
def test_local_factory_reset_ignores_pre_send_disconnect_event() -> None:
    """A stale disconnect published before the request is queued cannot prove acceptance."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=Acknowledgment(),
        _timeout=SimpleNamespace(expireTimeout=0.01),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is False
        main_module.pub.sendMessage("meshtastic.connection.lost", interface=iface)
        return SimpleNamespace(id=654)

    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(side_effect=_factory_reset),
    )

    with pytest.raises(
        RuntimeError,
        match="Timed out waiting for a factory reset acknowledgment or reboot disconnect",
    ):
        main_module._send_local_factory_reset_and_wait(
            reset_node,
            full=False,
            timeout=0.01,
        )

    iface._retire_wait_request.assert_called_once_with("receivedNak", request_id=654)


@pytest.mark.unit
def test_local_factory_reset_times_out_without_ack_or_disconnect() -> None:
    """A queued reset without ACK, NAK, or reboot remains a hard failure."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=Acknowledgment(),
        _timeout=SimpleNamespace(expireTimeout=0.01),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(return_value=SimpleNamespace(id=789)),
    )

    with pytest.raises(
        RuntimeError,
        match="Timed out waiting for a factory reset acknowledgment or reboot disconnect",
    ):
        main_module._send_local_factory_reset_and_wait(
            reset_node,
            full=False,
            timeout=0.01,
        )

    iface._retire_wait_request.assert_called_once_with("receivedNak", request_id=789)


@pytest.mark.unit
def test_local_factory_reset_nak_remains_failure() -> None:
    """A request-scoped routing error must win over any transport signal."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        socket=object(),
        stream=None,
        _acknowledgment=Acknowledgment(),
        _wait_for_request_ack=MagicMock(return_value=True),
        _raise_wait_error_if_present=MagicMock(
            side_effect=RuntimeError("Routing error on response: NOT_AUTHORIZED")
        ),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(return_value=SimpleNamespace(id=987)),
    )

    with pytest.raises(RuntimeError, match="Routing error on response: NOT_AUTHORIZED"):
        main_module._send_local_factory_reset_and_wait(
            reset_node,
            full=False,
            timeout=0.5,
        )

    iface._wait_for_request_ack.assert_called_once()
    iface._raise_wait_error_if_present.assert_called_once_with(
        "receivedNak", request_id=987
    )
    iface._retire_wait_request.assert_called_once_with("receivedNak", request_id=987)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_factory_reset_skips_ack_wait_when_send_is_disabled() -> None:
    """A noProto reset returning None must not enter an impossible ACK wait."""
    sys.argv = ["", "--factory-reset"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    reset_node = iface.getNode.return_value
    reset_node.iface = iface
    reset_node.factoryReset.return_value = None

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    iface._wait_for_request_ack.assert_not_called()
    iface._raise_wait_error_if_present.assert_not_called()
    iface._retire_wait_request.assert_not_called()
    iface.waitForAckNak.assert_not_called()


@pytest.mark.unit
def test_post_factory_reset_ready_probe_closes_and_probes_reconnect() -> None:
    iface = cast(Any, object.__new__(SerialInterface))
    iface.connect = MagicMock()
    iface.close = MagicMock()

    main_module._post_factory_reset_ready_probe(cast(Any, iface))

    iface.connect.assert_called_once()
    assert iface.close.call_count >= 2


@pytest.mark.unit
def test_post_factory_reset_ready_probe_bounds_and_quiets_expected_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A still-rebooting device should produce one concise info line, not a traceback."""
    iface = cast(Any, object.__new__(SerialInterface))
    iface.close = MagicMock()

    def _connect() -> None:
        assert iface._connect_wait_timeout_seconds == (
            main_module.FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS
        )
        assert iface._connect_retry_budget_seconds == (
            main_module.FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS
        )
        assert iface._suppress_connect_failure_logging is True
        raise RuntimeError("device still rebooting")

    iface.connect = MagicMock(side_effect=_connect)

    with caplog.at_level(logging.DEBUG):
        main_module._post_factory_reset_ready_probe(cast(Any, iface))

    assert "device is still rebooting" in caplog.text
    assert "Traceback" not in caplog.text
    assert not hasattr(iface, "_connect_wait_timeout_seconds")
    assert not hasattr(iface, "_connect_retry_budget_seconds")
    assert not hasattr(iface, "_suppress_connect_failure_logging")
    assert iface.close.call_count >= 2


@pytest.mark.unit
def test_temporary_instance_attributes_restores_existing_and_missing_values() -> None:
    instance = SimpleNamespace(existing="before")

    with main_module._temporary_instance_attributes(
        instance, {"existing": "during", "temporary": 42}
    ):
        assert instance.existing == "during"
        assert instance.temporary == 42

    assert instance.existing == "before"
    assert not hasattr(instance, "temporary")


@pytest.mark.unit
def test_temporary_instance_attributes_does_not_shadow_inherited_values() -> None:
    class WithInheritedValue:
        inherited = "class-value"

    instance = WithInheritedValue()

    with main_module._temporary_instance_attributes(
        instance, {"inherited": "temporary"}
    ):
        assert instance.inherited == "temporary"
        assert vars(instance)["inherited"] == "temporary"

    assert instance.inherited == "class-value"
    assert "inherited" not in vars(instance)


@pytest.mark.unit
def test_post_factory_reset_ready_probe_restores_existing_overrides() -> None:
    iface = cast(Any, object.__new__(SerialInterface))
    iface.close = MagicMock()
    iface.connect = MagicMock()
    iface._connect_wait_timeout_seconds = 11.0
    iface._connect_retry_budget_seconds = 12.0
    iface._suppress_connect_failure_logging = False

    main_module._post_factory_reset_ready_probe(cast(Any, iface))

    assert iface._connect_wait_timeout_seconds == 11.0
    assert iface._connect_retry_budget_seconds == 12.0
    assert iface._suppress_connect_failure_logging is False


@pytest.mark.unit
def test_post_factory_reset_ready_probe_logs_final_close_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    iface = cast(Any, object.__new__(SerialInterface))
    iface.connect = MagicMock()
    iface.close = MagicMock(side_effect=[None, RuntimeError("close failed")])

    with caplog.at_level(logging.DEBUG):
        main_module._post_factory_reset_ready_probe(cast(Any, iface))

    assert "Factory reset: final serial close failed" in caplog.text
