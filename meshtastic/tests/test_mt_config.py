"""Unit tests for mt_config module state and compatibility aliases."""

from __future__ import annotations

import argparse
import threading
import types
import warnings
from typing import Any

import pytest

from meshtastic import mt_config


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_reset_restores_module_defaults() -> None:
    """reset() should restore state keys to values defined in MODULE_STATE_DEFAULTS."""
    mt_config.args = argparse.Namespace(example=True)
    mt_config.channel_index = 7
    mt_config.camel_case = True
    mt_config.reset()

    for key, value in mt_config.MODULE_STATE_DEFAULTS.items():
        assert getattr(mt_config, key) == value


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_reset_clears_warned_deprecations() -> None:
    """reset() should clear the warn-once deprecation tracking."""
    # Trigger a deprecation warning
    with pytest.warns(DeprecationWarning):
        _ = mt_config.tunnelInstance  # pyright: ignore[reportAttributeAccessIssue]

    # Verify it was tracked
    assert "tunnelInstance" in mt_config._warned_deprecations

    # Reset should clear it
    mt_config.reset()
    assert "tunnelInstance" not in mt_config._warned_deprecations


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_reset_with_thread_safety() -> None:
    """reset() should work correctly with the deprecation lock."""
    # Set some state
    mt_config.channel_index = 5

    # Reset from multiple threads
    threads = []
    for _ in range(10):
        t = threading.Thread(target=mt_config.reset)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Should be reset to default
    assert mt_config.channel_index is None


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_getattr_compat_alias_emits_deprecation_warning() -> None:
    """Accessing tunnelInstance should return tunnel_instance and warn."""
    marker: Any = object()
    mt_config.tunnel_instance = marker
    with pytest.warns(DeprecationWarning):
        result = mt_config.tunnelInstance  # pyright: ignore[reportAttributeAccessIssue]
    assert result is marker


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_getattr_compat_alias_warn_once() -> None:
    """Accessing tunnelInstance should only warn once per process."""
    marker: Any = object()
    mt_config.tunnel_instance = marker

    # First access warns
    with pytest.warns(DeprecationWarning):
        _ = mt_config.tunnelInstance  # pyright: ignore[reportAttributeAccessIssue]

    # Second access does not warn
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _ = mt_config.tunnelInstance  # pyright: ignore[reportAttributeAccessIssue]
        assert len(caught) == 0


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_setattr_compat_alias_sets_new_name_and_warns() -> None:
    """Assigning tunnelInstance should route to tunnel_instance and warn."""
    marker: Any = object()
    with pytest.warns(DeprecationWarning):
        mt_config.tunnelInstance = marker  # pyright: ignore[reportAttributeAccessIssue]
    assert mt_config.tunnel_instance is marker


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_setattr_compat_alias_warn_once() -> None:
    """Setting tunnelInstance should only warn once per process."""
    marker1: Any = object()
    marker2: Any = object()

    # First set warns
    with pytest.warns(DeprecationWarning):
        mt_config.tunnelInstance = (
            marker1  # pyright: ignore[reportAttributeAccessIssue]
        )

    # Second set does not warn
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mt_config.tunnelInstance = (
            marker2  # pyright: ignore[reportAttributeAccessIssue]
        )
        assert len(caught) == 0

    assert mt_config.tunnel_instance is marker2


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_delattr_compat_alias_deletes_new_name_and_warns() -> None:
    """Deleting tunnelInstance should route to tunnel_instance and warn."""
    marker: Any = object()
    mt_config.tunnel_instance = marker

    with pytest.warns(DeprecationWarning):
        del mt_config.tunnelInstance  # pyright: ignore[reportAttributeAccessIssue]

    assert not hasattr(mt_config, "tunnel_instance")


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_delattr_compat_alias_warn_once() -> None:
    """Deleting tunnelInstance should only warn once per process."""
    marker: Any = object()
    mt_config.tunnel_instance = marker

    # First delete warns
    with pytest.warns(DeprecationWarning):
        del mt_config.tunnelInstance  # pyright: ignore[reportAttributeAccessIssue]

    # Reset and set again for second delete
    mt_config.tunnel_instance = marker

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        del mt_config.tunnelInstance  # pyright: ignore[reportAttributeAccessIssue]
        assert len(caught) == 0


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_getattr_unknown_name_raises_attribute_error() -> None:
    """Unknown mt_config attributes should raise AttributeError."""
    with pytest.raises(AttributeError):
        _ = mt_config.this_attribute_does_not_exist


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_setattr_unknown_name_sets_attribute() -> None:
    """Setting unknown attributes should work normally."""
    mt_config.new_attribute = (
        "test_value"  # pyright: ignore[reportAttributeAccessIssue]
    )
    assert (
        mt_config.new_attribute == "test_value"
    )  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_delattr_unknown_name_deletes_attribute() -> None:
    """Deleting unknown attributes should work normally."""
    mt_config.new_attribute = (
        "test_value"  # pyright: ignore[reportAttributeAccessIssue]
    )
    del mt_config.new_attribute  # pyright: ignore[reportAttributeAccessIssue]
    assert not hasattr(mt_config, "new_attribute")


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_module_state_defaults_complete() -> None:
    """All module state variables should have defaults."""
    expected_keys = {
        "args",
        "parser",
        "channel_index",
        "logfile",
        "tunnel_instance",
        "camel_case",
    }
    assert set(mt_config.MODULE_STATE_DEFAULTS.keys()) == expected_keys


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_module_state_defaults_types() -> None:
    """Module state defaults should have correct types."""
    assert mt_config.MODULE_STATE_DEFAULTS["args"] is None
    assert mt_config.MODULE_STATE_DEFAULTS["parser"] is None
    assert mt_config.MODULE_STATE_DEFAULTS["channel_index"] is None
    assert mt_config.MODULE_STATE_DEFAULTS["logfile"] is None
    assert mt_config.MODULE_STATE_DEFAULTS["tunnel_instance"] is None
    assert mt_config.MODULE_STATE_DEFAULTS["camel_case"] is False


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_state_isolation_between_tests_part1() -> None:
    """First test in isolation pair - set state."""
    mt_config.channel_index = 42
    assert mt_config.channel_index == 42


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_state_isolation_between_tests_part2() -> None:
    """Second test in isolation pair - verify state is reset."""
    # mt_config_state fixture should have reset the state
    assert mt_config.channel_index is None


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_compat_aliases_mapping() -> None:
    """Compatibility aliases mapping should be correct."""
    assert "tunnelInstance" in mt_config._COMPAT_ALIASES
    assert mt_config._COMPAT_ALIASES["tunnelInstance"] == "tunnel_instance"


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_concurrent_access_thread_safety() -> None:
    """Concurrent access to deprecation tracking should be thread-safe."""
    results = []

    def access_deprecated_alias() -> None:
        try:
            # Access the deprecated alias (warns once per process)
            _ = mt_config.tunnelInstance  # pyright: ignore[reportAttributeAccessIssue]
            results.append("success")
        except Exception as e:
            results.append(f"error: {e}")

    threads = [threading.Thread(target=access_deprecated_alias) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All threads should complete without errors
    assert len(results) == 20
    assert all(r == "success" for r in results)


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_setattr_normal_attribute() -> None:
    """Setting normal attributes should work without warnings."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mt_config.channel_index = 99
        assert len(caught) == 0

    assert mt_config.channel_index == 99


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_getattr_normal_attribute() -> None:
    """Getting normal attributes should work without warnings."""
    mt_config.channel_index = 99

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _ = mt_config.channel_index
        assert len(caught) == 0


@pytest.mark.unit
@pytest.mark.usefixtures("mt_config_state")
def test_module_class_is_mt_config_module() -> None:
    """Module should use custom _MtConfigModule class."""
    assert isinstance(mt_config, types.ModuleType)
    assert mt_config.__class__.__name__ == "_MtConfigModule"
