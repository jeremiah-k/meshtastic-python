from meshtastic.interfaces.ble import BLEClient, BLEInterface


def test_ble_interface_naming_compatibility():
    """Verify that legacy snake_case names are still available on BLEInterface."""
    # We don't need to actually connect, just check for attribute existence
    # Canonical camelCase
    assert hasattr(BLEInterface, "findDevice")
    assert hasattr(BLEInterface, "fromNumHandler")
    # Legacy snake_case aliases
    assert hasattr(BLEInterface, "find_device")
    assert hasattr(BLEInterface, "from_num_handler")


def test_ble_client_naming_compatibility():
    """Verify that legacy snake_case names are still available on BLEClient."""
    # Canonical camelCase
    assert hasattr(BLEClient, "isConnected")
    assert hasattr(BLEClient, "readGattChar")
    assert hasattr(BLEClient, "writeGattChar")
    assert hasattr(BLEClient, "getServices")
    assert hasattr(BLEClient, "hasCharacteristic")
    assert hasattr(BLEClient, "startNotify")
    assert hasattr(BLEClient, "stopNotify")

    # Legacy snake_case aliases
    assert hasattr(BLEClient, "is_connected")
    assert hasattr(BLEClient, "read_gatt_char")
    assert hasattr(BLEClient, "write_gatt_char")
    assert hasattr(BLEClient, "get_services")
    assert hasattr(BLEClient, "has_characteristic")
    assert hasattr(BLEClient, "start_notify")
    assert hasattr(BLEClient, "stop_notify")


def test_ble_diagnostic_naming_compatibility():
    """Verify that legacy snake_case diagnostics are still available."""
    from meshtastic.interfaces.ble.client import (
        get_zombie_thread_count,
        getZombieThreadCount,
    )
    from meshtastic.interfaces.ble.runner import (
        get_zombie_runner_count,
        getZombieRunnerCount,
    )

    assert getZombieThreadCount is not None
    assert get_zombie_thread_count is not None
    assert getZombieRunnerCount is not None
    assert get_zombie_runner_count is not None


def test_ble_module_level_naming_compatibility():
    """Verify that module-level aliases are still available."""
    from meshtastic.interfaces.ble import interface

    assert hasattr(interface, "addr_key")
    assert hasattr(interface, "is_currently_connected_elsewhere")
