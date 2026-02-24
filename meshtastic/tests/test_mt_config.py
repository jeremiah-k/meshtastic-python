"""Unit tests for mt_config module state and compatibility aliases."""

from __future__ import annotations

import argparse
import warnings
from typing import Any, cast

import pytest

from meshtastic import mt_config


@pytest.mark.unit
def test_reset_restores_module_defaults(mt_config_state: None) -> None:
    """reset() should restore state keys to values defined in MODULE_STATE_DEFAULTS."""
    _ = mt_config_state
    mt_config.args = argparse.Namespace(example=True)
    mt_config.channel_index = 7
    mt_config.camel_case = True
    mt_config.reset()

    for key, value in mt_config.MODULE_STATE_DEFAULTS.items():
        assert getattr(mt_config, key) == value


@pytest.mark.unit
def test_getattr_compat_alias_emits_deprecation_warning(mt_config_state: None) -> None:
    """Accessing tunnelInstance should return tunnel_instance and warn."""
    _ = mt_config_state
    marker = cast(Any, object())
    mt_config.tunnel_instance = marker
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert (
            mt_config.tunnelInstance is marker
        )  # pyright: ignore[reportAttributeAccessIssue]
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


@pytest.mark.unit
def test_setattr_compat_alias_sets_new_name_and_warns(mt_config_state: None) -> None:
    """Assigning tunnelInstance should route to tunnel_instance and warn."""
    _ = mt_config_state
    marker = cast(Any, object())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mt_config.tunnelInstance = marker  # pyright: ignore[reportAttributeAccessIssue]
    assert mt_config.tunnel_instance is marker
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


@pytest.mark.unit
def test_getattr_unknown_name_raises_attribute_error(mt_config_state: None) -> None:
    """Unknown mt_config attributes should raise AttributeError."""
    _ = mt_config_state
    with pytest.raises(AttributeError):
        _ = mt_config.this_attribute_does_not_exist  # type: ignore[attr-defined]
