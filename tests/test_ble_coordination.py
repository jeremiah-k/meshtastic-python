"""Coverage-focused tests for BLE thread coordination helpers."""

from unittest.mock import MagicMock

import pytest

try:
    from meshtastic.interfaces.ble.coordination import ThreadCoordinator
except ImportError:
    pytest.skip("BLE dependencies not available", allow_module_level=True)


@pytest.mark.unit
def test_join_all_joins_tracked_alive_threads() -> None:
    """_join_all should join each tracked alive thread."""
    coordinator = ThreadCoordinator()
    tracked = MagicMock()
    tracked.is_alive.return_value = True

    with coordinator._lock:
        coordinator._threads = [tracked]  # type: ignore[assignment]

    coordinator._join_all(timeout=0.25)

    tracked.join.assert_called_once_with(timeout=0.25)


@pytest.mark.unit
def test_assert_lock_owned_raises_when_lock_not_owned() -> None:
    """_assert_lock_owned should raise when a lock exposes _is_owned() == False."""

    class _LockStub:
        @staticmethod
        def _is_owned() -> bool:
            return False

    coordinator = ThreadCoordinator()
    coordinator._lock = _LockStub()  # type: ignore[assignment]

    with pytest.raises(
        RuntimeError, match=r"Expected ThreadCoordinator\._lock to be held"
    ):
        coordinator._assert_lock_owned()
