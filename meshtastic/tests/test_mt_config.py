"""Unit tests for mt_config module state and compatibility aliases."""

from __future__ import annotations

import argparse
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
def test_getattr_compat_alias_emits_deprecation_warning() -> None:
    """Accessing tunnelInstance should return tunnel_instance and warn."""
    marker: Any = object()
    mt_config.tunnel_instance = marker
    with pytest.warns(DeprecationWarning):
        result = mt_config.tunnelInstance  # pyright: ignore[reportAttributeAccessIssue]
    assert result is marker


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
def test_getattr_unknown_name_raises_attribute_error() -> None:
    """Unknown mt_config attributes should raise AttributeError."""
    with pytest.raises(AttributeError):
        _ = mt_config.this_attribute_does_not_exist
