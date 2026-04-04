from unittest.mock import MagicMock

import pytest

from meshtastic.interfaces.ble.interface import BLEInterface


def test_disconnect_delegates_to_close() -> None:
    iface = object.__new__(BLEInterface)
    iface.close = MagicMock()

    BLEInterface.disconnect(iface)

    iface.close.assert_called_once_with()


def test_disconnect_propagates_close_errors() -> None:
    iface = object.__new__(BLEInterface)
    iface.close = MagicMock(side_effect=RuntimeError("close failed"))

    with pytest.raises(RuntimeError, match="close failed"):
        BLEInterface.disconnect(iface)
