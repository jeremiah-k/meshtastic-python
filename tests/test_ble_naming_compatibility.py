"""_summary_."""

from meshtastic.interfaces.ble import BLEClient, BLEInterface


def test_ble_interface_naming_compatibility():
    """Verify BLEInterface exposes the intended compatibility/public method names."""
    # We don't need to actually connect, just check for attribute existence
    assert hasattr(BLEInterface, "findDevice")
    assert hasattr(BLEInterface, "find_device")
    assert hasattr(BLEInterface, "from_num_handler")
    assert hasattr(BLEInterface, "log_radio_handler")
    assert hasattr(BLEInterface, "legacy_log_radio_handler")


def test_ble_client_naming_compatibility():
    """Verify BLEClient exposes pre-refactor-compatible names plus selected promotions."""
    # Established public API on master
    assert hasattr(BLEClient, "discover")
    assert hasattr(BLEClient, "pair")
    assert hasattr(BLEClient, "connect")
    assert hasattr(BLEClient, "disconnect")
    assert hasattr(BLEClient, "read_gatt_char")
    assert hasattr(BLEClient, "write_gatt_char")
    assert hasattr(BLEClient, "has_characteristic")
    assert hasattr(BLEClient, "start_notify")
    assert hasattr(BLEClient, "close")
    assert hasattr(BLEClient, "async_await")
    assert hasattr(BLEClient, "async_run")
    # Explicit low-risk promotions
    assert hasattr(BLEClient, "isConnected")
    assert hasattr(BLEClient, "stopNotify")
    assert hasattr(BLEClient, "is_connected")
    assert hasattr(BLEClient, "stop_notify")


def test_ble_diagnostic_naming_compatibility():
    """Verify that snake_case diagnostics are available."""
    from meshtastic.interfaces.ble.client import get_zombie_thread_count
    from meshtastic.interfaces.ble.runner import get_zombie_runner_count

    assert get_zombie_thread_count is not None
    assert get_zombie_runner_count is not None


def test_ble_module_level_naming_compatibility():
    """Verify internal gating helpers are not leaked as interface module aliases."""
    from meshtastic.interfaces.ble import interface

    assert not hasattr(interface, "addr_key")
    assert not hasattr(interface, "is_currently_connected_elsewhere")
