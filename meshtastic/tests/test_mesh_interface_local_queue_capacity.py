"""Regression coverage for local-loopback TX queue capacity handling."""

from collections import OrderedDict
from collections.abc import Callable
from threading import RLock
from unittest.mock import MagicMock

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime.queue_send import (
    QUEUE_WAIT_TIMEOUT_SECONDS,
    QueueWaitError,
    _QueueSendRuntime,
)
from meshtastic.protobuf import mesh_pb2


LOCAL_NUM = 0x12345678
REMOTE_NUM = 0x87654321


class _QueueHarness:
    """Minimal queue state used by queue-runtime capacity tests."""

    def __init__(self, *, free: int) -> None:
        self.lock = RLock()
        self.queue: OrderedDict[int, mesh_pb2.ToRadio | bool] = OrderedDict()
        self.queue_status: mesh_pb2.QueueStatus | None = mesh_pb2.QueueStatus(
            free=free,
            maxlen=16,
        )

    def set_queue_status(self, status: mesh_pb2.QueueStatus) -> None:
        """Replace the cached queue status."""
        self.queue_status = status


def _packet(packet_id: int, destination: int) -> mesh_pb2.ToRadio:
    """Build one packet-bearing ToRadio message."""
    message = mesh_pb2.ToRadio()
    message.packet.id = packet_id
    message.packet.to = destination
    return message


def _runtime(
    harness: _QueueHarness,
    *,
    local_num: int = LOCAL_NUM,
    queue_wait_delay_seconds: float = 0.0,
    queue_wait_timeout_seconds: float = QUEUE_WAIT_TIMEOUT_SECONDS,
    classifier: Callable[[mesh_pb2.ToRadio], bool] | None = None,
    omit_classifier: bool = False,
) -> _QueueSendRuntime:
    """Build a queue runtime with the local-loopback classifier used by tests."""
    if omit_classifier:
        return _QueueSendRuntime(
            lock=harness.lock,
            get_queue=lambda: harness.queue,
            get_queue_status=lambda: harness.queue_status,
            set_queue_status=harness.set_queue_status,
            queue_wait_delay_seconds=queue_wait_delay_seconds,
            queue_wait_timeout_seconds=queue_wait_timeout_seconds,
        )
    if classifier is None:
        def classifier(message: mesh_pb2.ToRadio) -> bool:
            return message.packet.to != local_num

    return _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=queue_wait_delay_seconds,
        queue_wait_timeout_seconds=queue_wait_timeout_seconds,
        uses_tx_queue_capacity=classifier,
    )


@pytest.mark.unit
def test_local_loopback_sends_when_firmware_rf_queue_is_full() -> None:
    """Local loopback must not wait for unrelated firmware RF queue capacity."""
    local_num = LOCAL_NUM
    harness = _QueueHarness(free=0)
    sent: list[int] = []
    sleep = MagicMock()
    runtime = _runtime(
        harness,
        queue_wait_delay_seconds=0.5,
        queue_wait_timeout_seconds=0.0,
    )

    runtime.send_to_radio(
        _packet(101, local_num),
        send_impl=lambda message: sent.append(message.packet.id),
        sleep_fn=sleep,
    )

    assert sent == [101]
    assert harness.queue_status is not None
    assert harness.queue_status.free == 0
    assert list(harness.queue) == []
    sleep.assert_not_called()


@pytest.mark.unit
def test_local_loopback_returns_without_draining_blocked_rf_backlog() -> None:
    """A local send must not inherit waiting responsibility for RF backlog."""
    local_num = LOCAL_NUM
    remote_num = REMOTE_NUM
    harness = _QueueHarness(free=0)
    remote = _packet(201, remote_num)
    local = _packet(202, local_num)
    harness.queue[remote.packet.id] = remote
    sent: list[int] = []
    sleep = MagicMock()

    runtime = _runtime(
        harness,
        queue_wait_delay_seconds=0.5,
        queue_wait_timeout_seconds=0.0,
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
    local_num = LOCAL_NUM
    harness = _QueueHarness(free=3)
    runtime = _runtime(harness)

    runtime.send_to_radio(
        _packet(301, local_num),
        send_impl=lambda _message: None,
        sleep_fn=lambda _seconds: None,
    )

    assert harness.queue_status is not None
    assert harness.queue_status.free == 3


@pytest.mark.unit
def test_failed_local_loopback_send_does_not_restore_rf_capacity() -> None:
    """A failed local transport send must not fabricate a free RF queue slot."""
    local_num = LOCAL_NUM
    harness = _QueueHarness(free=0)
    packet = _packet(350, local_num)
    runtime = _runtime(harness)

    with pytest.raises(RuntimeError, match="transport failed"):
        runtime.send_to_radio(
            packet,
            send_impl=lambda _message: (_ for _ in ()).throw(
                RuntimeError("transport failed")
            ),
            sleep_fn=lambda _seconds: None,
        )

    assert harness.queue_status is not None
    assert harness.queue_status.free == 0
    assert list(harness.queue) == [350]
    assert 350 not in runtime._awaiting_queue_status_ids


@pytest.mark.unit
def test_failed_older_local_retry_stays_ahead_of_newer_local_work() -> None:
    """A failed local retry must retain its order ahead of newer local packets."""
    local_num = LOCAL_NUM
    remote_num = REMOTE_NUM
    harness = _QueueHarness(free=0)
    harness.queue[360] = _packet(360, remote_num)
    harness.queue[361] = _packet(361, local_num)
    attempted: list[int] = []
    runtime = _runtime(harness)

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
    local_num = LOCAL_NUM
    remote_num = REMOTE_NUM
    harness = _QueueHarness(free=0)
    harness.queue[330] = _packet(330, remote_num)
    harness.queue[331] = _packet(331, local_num)
    sent: list[int] = []
    runtime = _runtime(harness)

    runtime.send_to_radio(
        _packet(332, local_num),
        send_impl=lambda message: sent.append(message.packet.id),
        sleep_fn=lambda _seconds: None,
    )

    assert sent == [331, 332]
    assert list(harness.queue) == [330]
    assert harness.queue_status is not None
    assert harness.queue_status.free == 0


@pytest.mark.unit
def test_delayed_local_queue_status_does_not_leave_ack_sentinel() -> None:
    """Delayed QueueStatus replies should retire local correlation cleanly."""
    local_num = LOCAL_NUM
    harness = _QueueHarness(free=0)
    runtime = _runtime(harness)

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
    assert 375 not in runtime._packet_uses_capacity_by_id


@pytest.mark.unit
def test_synchronous_local_queue_status_correlates_during_send() -> None:
    """A QueueStatus delivered during transport I/O must not be re-registered."""
    local_num = LOCAL_NUM
    harness = _QueueHarness(free=0)
    runtime = _runtime(harness)

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
    assert 376 not in runtime._packet_uses_capacity_by_id


@pytest.mark.unit
@pytest.mark.parametrize("result", (0, 1))
def test_synchronous_remote_queue_status_cleans_capacity_state(result: int) -> None:
    """A synchronous remote status must terminate ownership correlation cleanly."""
    harness = _QueueHarness(free=1)
    runtime = _runtime(harness)

    def send_and_reply(message: mesh_pb2.ToRadio) -> None:
        runtime.handle_queue_status_from_radio(
            mesh_pb2.QueueStatus(
                free=1,
                maxlen=16,
                res=result,
                mesh_packet_id=message.packet.id,
            )
        )

    runtime.send_to_radio(
        _packet(377, REMOTE_NUM),
        send_impl=send_and_reply,
        sleep_fn=lambda _seconds: None,
    )

    assert list(harness.queue) == []
    assert 377 not in runtime._awaiting_queue_status_ids
    assert 377 not in runtime._packet_uses_capacity_by_id
    assert 377 not in runtime._claimed_queue_status_by_id


@pytest.mark.unit
def test_runtime_without_classifier_keeps_historical_backpressure() -> None:
    """Omitting the classifier must preserve RF-capacity waits for every packet."""
    harness = _QueueHarness(free=0)
    send_impl = MagicMock()
    runtime = _runtime(
        harness,
        queue_wait_timeout_seconds=0.0,
        omit_classifier=True,
    )

    with pytest.raises(QueueWaitError, match="free space in TX queue"):
        runtime.send_to_radio(
            _packet(601, LOCAL_NUM),
            send_impl=send_impl,
            sleep_fn=lambda _seconds: None,
        )

    send_impl.assert_not_called()
    assert list(harness.queue) == []


@pytest.mark.unit
def test_incoming_capacity_classification_is_snapshotted_once() -> None:
    """Reconnect-time classifier changes must not strand an accepted local packet."""
    harness = _QueueHarness(free=0)
    calls = 0

    def changing_classifier(_message: mesh_pb2.ToRadio) -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    runtime = _runtime(harness, classifier=changing_classifier)
    sent: list[int] = []

    runtime.send_to_radio(
        _packet(602, LOCAL_NUM),
        send_impl=lambda message: sent.append(message.packet.id),
        sleep_fn=lambda _seconds: None,
    )

    assert sent == [602]
    assert calls == 1
    assert list(harness.queue) == []


@pytest.mark.unit
def test_failed_local_retry_keeps_snapshotted_exemption() -> None:
    """A failed local packet must remain exempt after classifier state changes."""
    harness = _QueueHarness(free=0)
    uses_capacity = False
    runtime = _runtime(
        harness,
        classifier=lambda _message: uses_capacity,
        queue_wait_timeout_seconds=0.0,
    )

    with pytest.raises(RuntimeError, match="transport failed"):
        runtime.send_to_radio(
            _packet(603, LOCAL_NUM),
            send_impl=lambda _message: (_ for _ in ()).throw(
                RuntimeError("transport failed")
            ),
            sleep_fn=lambda _seconds: None,
        )

    uses_capacity = True
    sent: list[int] = []
    with pytest.raises(QueueWaitError, match="free space in TX queue"):
        runtime.send_to_radio(
            _packet(604, REMOTE_NUM),
            send_impl=lambda message: sent.append(message.packet.id),
            sleep_fn=lambda _seconds: None,
        )

    assert sent == [603]
    assert list(harness.queue) == []


@pytest.mark.unit
def test_failed_send_restores_only_an_actually_claimed_slot() -> None:
    """A late QueueStatus must not fabricate capacity after an unclaimed send."""
    harness = _QueueHarness(free=0)
    harness.queue_status = None
    runtime = _runtime(harness)

    def fail_after_status_arrives(_message: mesh_pb2.ToRadio) -> None:
        harness.queue_status = mesh_pb2.QueueStatus(free=0, maxlen=16)
        raise RuntimeError("transport failed")

    with pytest.raises(RuntimeError, match="transport failed"):
        runtime.send_to_radio(
            _packet(605, REMOTE_NUM),
            send_impl=fail_after_status_arrives,
            sleep_fn=lambda _seconds: None,
        )

    assert harness.queue_status is not None
    assert harness.queue_status.free == 0
    assert list(harness.queue) == [605]
    assert 605 not in runtime._claimed_queue_status_by_id


@pytest.mark.unit
def test_failed_send_restores_a_slot_that_was_claimed() -> None:
    """A transport failure must restore RF capacity claimed for that attempt."""
    harness = _QueueHarness(free=1)
    runtime = _runtime(harness)

    with pytest.raises(RuntimeError, match="transport failed"):
        runtime.send_to_radio(
            _packet(606, REMOTE_NUM),
            send_impl=lambda _message: (_ for _ in ()).throw(
                RuntimeError("transport failed")
            ),
            sleep_fn=lambda _seconds: None,
        )

    assert harness.queue_status is not None
    assert harness.queue_status.free == 1
    assert list(harness.queue) == [606]
    assert 606 not in runtime._claimed_queue_status_by_id


@pytest.mark.unit
def test_failed_send_does_not_restore_into_newer_queue_status() -> None:
    """A newer firmware QueueStatus snapshot must supersede a local slot claim."""
    harness = _QueueHarness(free=1)
    runtime = _runtime(harness)

    def fail_after_new_status(_message: mesh_pb2.ToRadio) -> None:
        harness.queue_status = mesh_pb2.QueueStatus(free=4, maxlen=16)
        raise RuntimeError("transport failed")

    with pytest.raises(RuntimeError, match="transport failed"):
        runtime.send_to_radio(
            _packet(607, REMOTE_NUM),
            send_impl=fail_after_new_status,
            sleep_fn=lambda _seconds: None,
        )

    assert harness.queue_status is not None
    assert harness.queue_status.free == 4
    assert list(harness.queue) == [607]
    assert 607 not in runtime._claimed_queue_status_by_id


@pytest.mark.unit
def test_mesh_interface_classifies_only_known_local_destination_as_exempt() -> None:
    """MeshInterface should conservatively gate packets until local identity is known."""
    iface = object.__new__(MeshInterface)
    iface.myInfo = None
    local_num = LOCAL_NUM
    local = _packet(401, local_num)

    assert iface._packet_uses_tx_queue_capacity(local) is True

    iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=local_num)

    assert iface._packet_uses_tx_queue_capacity(local) is False
    assert iface._packet_uses_tx_queue_capacity(_packet(402, REMOTE_NUM)) is True
    assert iface._packet_uses_tx_queue_capacity(mesh_pb2.ToRadio()) is True


@pytest.mark.unit
def test_mesh_interface_sends_known_local_packet_with_full_rf_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MeshInterface should bypass RF backpressure for a known local destination."""
    monkeypatch.setattr(MeshInterface, "_queue_wait_timeout_seconds", 0.0)
    iface = MeshInterface()
    local_num = LOCAL_NUM
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
        iface._send_to_radio(_packet(502, REMOTE_NUM))

    send_impl.assert_not_called()
    assert list(iface.queue) == []
    assert 502 not in iface._queue_send_runtime._packet_uses_capacity_by_id
