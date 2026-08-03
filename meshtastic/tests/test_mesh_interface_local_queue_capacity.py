"""Regression coverage for local-loopback TX queue capacity handling."""

from collections import OrderedDict
from threading import RLock
from unittest.mock import MagicMock

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime
from meshtastic.protobuf import mesh_pb2


class _QueueHarness:
    """Minimal queue state used by queue-runtime capacity tests."""

    def __init__(self, *, free: int) -> None:
        self.lock = RLock()
        self.queue: OrderedDict[int, mesh_pb2.ToRadio | bool] = OrderedDict()
        self.queue_status = mesh_pb2.QueueStatus(free=free, maxlen=16)

    def set_queue_status(self, status: mesh_pb2.QueueStatus) -> None:
        """Replace the cached queue status."""
        self.queue_status = status


def _packet(packet_id: int, destination: int) -> mesh_pb2.ToRadio:
    """Build one packet-bearing ToRadio message."""
    message = mesh_pb2.ToRadio()
    message.packet.id = packet_id
    message.packet.to = destination
    return message


@pytest.mark.unit
def test_local_loopback_sends_when_firmware_rf_queue_is_full() -> None:
    """Local loopback must not wait for unrelated firmware RF queue capacity."""
    local_num = 0x12345678
    harness = _QueueHarness(free=0)
    sent: list[int] = []
    sleep = MagicMock()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.5,
        queue_wait_timeout_seconds=0.0,
        uses_tx_queue_capacity=lambda message: message.packet.to != local_num,
    )

    runtime.send_to_radio(
        _packet(101, local_num),
        send_impl=lambda message: sent.append(message.packet.id),
        sleep_fn=sleep,
    )

    assert sent == [101]
    assert harness.queue_status.free == 0
    assert list(harness.queue) == []
    sleep.assert_not_called()


@pytest.mark.unit
def test_local_loopback_returns_without_draining_blocked_rf_backlog() -> None:
    """A local send must not inherit waiting responsibility for RF backlog."""
    local_num = 0x12345678
    remote_num = 0x87654321
    harness = _QueueHarness(free=0)
    remote = _packet(201, remote_num)
    local = _packet(202, local_num)
    harness.queue[remote.packet.id] = remote
    sent: list[int] = []
    sleep = MagicMock()

    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.5,
        queue_wait_timeout_seconds=0.0,
        uses_tx_queue_capacity=lambda message: message.packet.to != local_num,
    )

    runtime.send_to_radio(
        local,
        send_impl=lambda message: sent.append(message.packet.id),
        sleep_fn=sleep,
    )

    assert sent == [202]
    assert list(harness.queue) == [201]
    sleep.assert_not_called()


@pytest.mark.unit
def test_local_loopback_does_not_claim_available_rf_capacity() -> None:
    """Sending locally must leave cached RF queue free-space unchanged."""
    local_num = 0x12345678
    harness = _QueueHarness(free=3)
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
        uses_tx_queue_capacity=lambda message: message.packet.to != local_num,
    )

    runtime.send_to_radio(
        _packet(301, local_num),
        send_impl=lambda _message: None,
        sleep_fn=lambda _seconds: None,
    )

    assert harness.queue_status.free == 3


@pytest.mark.unit
def test_failed_local_loopback_send_does_not_restore_rf_capacity() -> None:
    """A failed local transport send must not fabricate a free RF queue slot."""
    local_num = 0x12345678
    harness = _QueueHarness(free=0)
    packet = _packet(350, local_num)
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
        uses_tx_queue_capacity=lambda message: message.packet.to != local_num,
    )

    with pytest.raises(RuntimeError, match="transport failed"):
        runtime.send_to_radio(
            packet,
            send_impl=lambda _message: (_ for _ in ()).throw(
                RuntimeError("transport failed")
            ),
            sleep_fn=lambda _seconds: None,
        )

    assert harness.queue_status.free == 0
    assert list(harness.queue) == [350]
    assert 350 not in runtime._awaiting_queue_status_ids


@pytest.mark.unit
def test_failed_older_local_retry_stays_ahead_of_newer_local_work() -> None:
    """A failed local retry must retain its order ahead of newer local packets."""
    local_num = 0x12345678
    remote_num = 0x87654321
    harness = _QueueHarness(free=0)
    harness.queue[360] = _packet(360, remote_num)
    harness.queue[361] = _packet(361, local_num)
    attempted: list[int] = []
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
        uses_tx_queue_capacity=lambda message: message.packet.to != local_num,
    )

    def fail_send(message: mesh_pb2.ToRadio) -> None:
        attempted.append(message.packet.id)
        raise RuntimeError("transport failed")

    with pytest.raises(RuntimeError, match="transport failed"):
        runtime.send_to_radio(
            _packet(362, local_num),
            send_impl=fail_send,
            sleep_fn=lambda _seconds: None,
        )

    assert attempted == [361]
    sent: list[int] = []
    runtime.send_to_radio(
        _packet(363, local_num),
        send_impl=lambda message: sent.append(message.packet.id),
        sleep_fn=lambda _seconds: None,
    )

    assert sent == [361, 362, 363]
    assert list(harness.queue) == [360]


@pytest.mark.unit
def test_next_local_send_retries_older_local_backlog_without_rf_wait() -> None:
    """A later local send should retry older local backlog without touching RF work."""
    local_num = 0x12345678
    remote_num = 0x87654321
    harness = _QueueHarness(free=0)
    harness.queue[330] = _packet(330, remote_num)
    harness.queue[331] = _packet(331, local_num)
    sent: list[int] = []
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
        uses_tx_queue_capacity=lambda message: message.packet.to != local_num,
    )

    runtime.send_to_radio(
        _packet(332, local_num),
        send_impl=lambda message: sent.append(message.packet.id),
        sleep_fn=lambda _seconds: None,
    )

    assert sent == [331, 332]
    assert list(harness.queue) == [330]
    assert harness.queue_status.free == 0


@pytest.mark.unit
def test_delayed_local_queue_status_does_not_leave_ack_sentinel() -> None:
    """Delayed QueueStatus replies should retire local correlation cleanly."""
    local_num = 0x12345678
    harness = _QueueHarness(free=0)
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
        uses_tx_queue_capacity=lambda message: message.packet.to != local_num,
    )

    runtime.send_to_radio(
        _packet(375, local_num),
        send_impl=lambda _message: None,
        sleep_fn=lambda _seconds: None,
    )
    assert 375 in runtime._awaiting_queue_status_ids

    runtime.handle_queue_status_from_radio(
        mesh_pb2.QueueStatus(free=0, maxlen=16, mesh_packet_id=375)
    )

    assert list(harness.queue) == []
    assert 375 not in runtime._awaiting_queue_status_ids


@pytest.mark.unit
def test_synchronous_local_queue_status_correlates_during_send() -> None:
    """A QueueStatus delivered during transport I/O must not be re-registered."""
    local_num = 0x12345678
    harness = _QueueHarness(free=0)
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
        uses_tx_queue_capacity=lambda message: message.packet.to != local_num,
    )

    def send_and_reply(message: mesh_pb2.ToRadio) -> None:
        runtime.handle_queue_status_from_radio(
            mesh_pb2.QueueStatus(
                free=0,
                maxlen=16,
                mesh_packet_id=message.packet.id,
            )
        )

    runtime.send_to_radio(
        _packet(376, local_num),
        send_impl=send_and_reply,
        sleep_fn=lambda _seconds: None,
    )

    assert list(harness.queue) == []
    assert 376 not in runtime._awaiting_queue_status_ids


@pytest.mark.unit
def test_mesh_interface_classifies_only_known_local_destination_as_exempt() -> None:
    """MeshInterface should conservatively gate packets until local identity is known."""
    iface = object.__new__(MeshInterface)
    iface.myInfo = None
    local_num = 0x12345678
    local = _packet(401, local_num)

    assert iface._packet_uses_tx_queue_capacity(local) is True

    iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=local_num)

    assert iface._packet_uses_tx_queue_capacity(local) is False
    assert iface._packet_uses_tx_queue_capacity(_packet(402, 0x87654321)) is True
    assert iface._packet_uses_tx_queue_capacity(mesh_pb2.ToRadio()) is True


@pytest.mark.unit
def test_mesh_interface_sends_known_local_packet_with_full_rf_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MeshInterface should bypass RF backpressure for a known local destination."""
    monkeypatch.setattr(MeshInterface, "_queue_wait_timeout_seconds", 0.0)
    iface = MeshInterface()
    local_num = 0x12345678
    iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=local_num)
    iface.queueStatus = mesh_pb2.QueueStatus(free=0, maxlen=16)
    send_impl = MagicMock()
    monkeypatch.setattr(iface, "_send_to_radio_impl", send_impl)
    local = _packet(501, local_num)

    iface._send_to_radio(local)

    send_impl.assert_called_once_with(local)
    assert iface.queueStatus.free == 0


@pytest.mark.unit
def test_mesh_interface_still_blocks_remote_packet_when_rf_queue_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote packets must retain bounded firmware RF queue backpressure."""
    monkeypatch.setattr(MeshInterface, "_queue_wait_timeout_seconds", 0.0)
    iface = MeshInterface()
    iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=0x12345678)
    iface.queueStatus = mesh_pb2.QueueStatus(free=0, maxlen=16)
    send_impl = MagicMock()
    monkeypatch.setattr(iface, "_send_to_radio_impl", send_impl)

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="free space in TX queue"
    ):
        iface._send_to_radio(_packet(502, 0x87654321))

    send_impl.assert_not_called()
