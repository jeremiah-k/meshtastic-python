"""Edge coverage tests for tunnel initialization branches.

This module intentionally lives under ``tests/`` (not ``meshtastic/tests``)
because it isolates import-time tunnel behavior with temporary ``sys.modules``
patching used by
``test_tunnel_initialization_creates_tap_device_when_proto_enabled``.
"""

import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from meshtastic import mt_config


@pytest.mark.unit
def test_tunnel_initialization_creates_tap_device_when_proto_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tunnel should create/configure TapDevice when noProto is disabled."""
    tap_events: list[tuple[object, ...]] = []

    class _FakeTapDevice:
        def __init__(self, *, name: str) -> None:
            tap_events.append(("init", name))

        def up(self) -> None:
            tap_events.append(("up",))

        def ifconfig(self, *, address: str, netmask: str, mtu: int) -> None:
            tap_events.append(("ifconfig", address, netmask, mtu))

        def close(self) -> None:
            tap_events.append(("close",))

    class _FakeThread:
        def start(self) -> None:
            tap_events.append(("thread-start",))

        def join(self, timeout: float | None = None) -> None:
            _ = timeout

        def is_alive(self) -> bool:
            return False

    imported_with_fake_pytap2 = False
    try:
        tunnel_module = importlib.import_module("meshtastic.tunnel")
    except ModuleNotFoundError as exc:
        if exc.name != "pytap2":
            raise
        fake_pytap2 = types.ModuleType("pytap2")
        fake_pytap2.TapDevice = _FakeTapDevice
        monkeypatch.setitem(sys.modules, "pytap2", fake_pytap2)
        tunnel_module = importlib.import_module("meshtastic.tunnel")
        imported_with_fake_pytap2 = True
    else:
        monkeypatch.setattr(tunnel_module, "TapDevice", _FakeTapDevice)
    monkeypatch.setattr(tunnel_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        tunnel_module.threading,
        "Thread",
        lambda *_args, **_kwargs: _FakeThread(),
    )
    monkeypatch.setattr(tunnel_module.pub, "subscribe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tunnel_module.pub,
        "unsubscribe",
        lambda *_args, **_kwargs: None,
    )

    iface = SimpleNamespace(
        myInfo=SimpleNamespace(my_node_num=2475227164),
        nodes={},
        noProto=False,
        sendData=lambda *_args, **_kwargs: None,
    )

    tunnel = None
    try:
        tunnel = tunnel_module.Tunnel(iface)
        assert ("init", "mesh") in tap_events
        assert ("up",) in tap_events
        assert ("ifconfig", "10.115.248.28", "255.255.0.0", 200) in tap_events
        assert ("thread-start",) in tap_events
    finally:
        if tunnel is not None:
            tunnel.close()
        if imported_with_fake_pytap2:
            sys.modules.pop("meshtastic.tunnel", None)
            sys.modules.pop("pytap2", None)
        mt_config.reset()
