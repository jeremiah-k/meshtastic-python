"""Tests for BLE constants module."""

import pytest

try:
    from meshtastic.interfaces.ble import constants as ble_constants
except ImportError:
    pytest.skip("BLE dependencies not available", allow_module_level=True)


@pytest.mark.unit
def test_ble_constants_uuid_values():
    """Verify that BLE UUID constants have expected values."""
    assert ble_constants.SERVICE_UUID == "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
    assert ble_constants.TORADIO_UUID == "f75c76d2-129e-4dad-a1dd-7866124401e7"
    assert ble_constants.FROMRADIO_UUID == "2c55e69e-4993-11ed-b878-0242ac120002"
    assert ble_constants.FROMNUM_UUID == "ed9da18c-a800-4f66-a670-aa7547e34453"
    assert ble_constants.LEGACY_LOGRADIO_UUID == "6c6fd238-78fa-436b-aacf-15c5be1ef2e2"
    assert ble_constants.LOGRADIO_UUID == "5a3d6e49-06e6-4423-9944-e9de8cdf9547"


@pytest.mark.unit
def test_ble_constants_timeout_values():
    """Verify that timeout constants have expected values."""
    assert ble_constants.DISCONNECT_TIMEOUT_SECONDS == 5.0
    assert ble_constants.RECEIVE_THREAD_JOIN_TIMEOUT == 2.0
    assert ble_constants.EVENT_THREAD_JOIN_TIMEOUT == 2.0


@pytest.mark.unit
def test_ble_config_class_attributes():
    """Verify that BLEConfig class has expected attributes."""
    assert hasattr(ble_constants.BLEConfig, "BLE_SCAN_TIMEOUT")
    assert hasattr(ble_constants.BLEConfig, "RECEIVE_WAIT_TIMEOUT")
    assert hasattr(ble_constants.BLEConfig, "CONNECTION_TIMEOUT")
    assert hasattr(ble_constants.BLEConfig, "GATT_IO_TIMEOUT")

    # Verify specific values
    assert ble_constants.BLEConfig.BLE_SCAN_TIMEOUT == 10.0
    assert ble_constants.BLEConfig.RECEIVE_WAIT_TIMEOUT == 0.5
    assert ble_constants.BLEConfig.CONNECTION_TIMEOUT == 60.0
    assert ble_constants.BLEConfig.GATT_IO_TIMEOUT == 10.0


@pytest.mark.unit
def test_ble_constants_module_level_aliases():
    """Verify that module-level constants are aliases for BLEConfig attributes."""
    assert ble_constants.BLE_SCAN_TIMEOUT == ble_constants.BLEConfig.BLE_SCAN_TIMEOUT
    assert (
        ble_constants.RECEIVE_WAIT_TIMEOUT
        == ble_constants.BLEConfig.RECEIVE_WAIT_TIMEOUT
    )
    assert (
        ble_constants.CONNECTION_TIMEOUT == ble_constants.BLEConfig.CONNECTION_TIMEOUT
    )
    assert ble_constants.GATT_IO_TIMEOUT == ble_constants.BLEConfig.GATT_IO_TIMEOUT


@pytest.mark.unit
def test_ble_constants_getattr_delegates_to_bleconfig():
    """Verify that __getattr__ delegates unknown attributes to BLEConfig."""
    # Access an attribute that exists on BLEConfig but wasn't explicitly aliased
    assert hasattr(ble_constants, "RUNNER_LOOP_READY_TIMEOUT_SECONDS")
    value = ble_constants.RUNNER_LOOP_READY_TIMEOUT_SECONDS
    assert value == ble_constants.BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS


@pytest.mark.unit
def test_ble_constants_getattr_raises_for_unknown():
    """Verify that __getattr__ raises AttributeError for truly unknown attributes."""
    with pytest.raises(
        AttributeError, match="module .* has no attribute 'NONEXISTENT_CONSTANT'"
    ):
        _ = ble_constants.NONEXISTENT_CONSTANT


@pytest.mark.unit
def test_ble_error_message_constants():
    """Verify that error message constants are properly defined."""
    assert "timed out" in ble_constants.ERROR_TIMEOUT
    assert "Multiple Meshtastic BLE peripherals" in ble_constants.ERROR_MULTIPLE_DEVICES
    assert "Error reading BLE" == ble_constants.ERROR_READING_BLE
    assert "No Meshtastic BLE peripheral" in ble_constants.ERROR_NO_PERIPHERAL_FOUND
    assert "Error writing BLE" in ble_constants.ERROR_WRITING_BLE
    assert "Connection failed" in ble_constants.ERROR_CONNECTION_FAILED
    assert "No Meshtastic BLE peripherals found" in ble_constants.ERROR_NO_PERIPHERALS_FOUND


@pytest.mark.unit
def test_bleclient_constants():
    """Verify BLEClient-specific constants are defined."""
    assert (
        ble_constants.BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT
        == ble_constants.BLEConfig.BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT
    )
    assert ble_constants.BLECLIENT_ERROR_ASYNC_TIMEOUT == "Async operation timed out"


@pytest.mark.unit
def test_ble_config_runner_constants():
    """Verify BLEConfig has runner-related constants."""
    assert hasattr(ble_constants.BLEConfig, "RUNNER_LOOP_READY_TIMEOUT_SECONDS")
    assert hasattr(ble_constants.BLEConfig, "RUNNER_ZOMBIE_WARN_THRESHOLD")
    assert hasattr(ble_constants.BLEConfig, "RUNNER_SHUTDOWN_TIMEOUT_SECONDS")
    assert hasattr(ble_constants.BLEConfig, "RUNNER_IDLE_WAKE_INTERVAL_SECONDS")

    assert ble_constants.BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS == 5.0
    assert ble_constants.BLEConfig.RUNNER_ZOMBIE_WARN_THRESHOLD == 3
    assert ble_constants.BLEConfig.RUNNER_SHUTDOWN_TIMEOUT_SECONDS == 2.0
    assert ble_constants.BLEConfig.RUNNER_IDLE_WAKE_INTERVAL_SECONDS == 0.1


@pytest.mark.unit
def test_ble_config_reconnect_constants():
    """Verify BLEConfig has reconnect-related constants."""
    assert hasattr(ble_constants.BLEConfig, "AUTO_RECONNECT_INITIAL_DELAY")
    assert hasattr(ble_constants.BLEConfig, "AUTO_RECONNECT_MAX_DELAY")
    assert hasattr(ble_constants.BLEConfig, "AUTO_RECONNECT_BACKOFF")
    assert hasattr(ble_constants.BLEConfig, "AUTO_RECONNECT_JITTER_RATIO")

    assert ble_constants.BLEConfig.AUTO_RECONNECT_INITIAL_DELAY == 1.0
    assert ble_constants.BLEConfig.AUTO_RECONNECT_MAX_DELAY == 30.0
    assert ble_constants.BLEConfig.AUTO_RECONNECT_BACKOFF == 2.0
    assert ble_constants.BLEConfig.AUTO_RECONNECT_JITTER_RATIO == 0.15


@pytest.mark.unit
def test_logger_exists():
    """Verify that the BLE logger is properly configured."""
    assert hasattr(ble_constants, "logger")
    logger = ble_constants.logger
    assert logger is not None
    assert logger.name == "meshtastic.ble"
