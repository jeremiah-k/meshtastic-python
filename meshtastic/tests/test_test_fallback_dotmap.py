"""Unit tests for the fallback DotMap compatibility shim in meshtastic.test."""

from __future__ import annotations

import pytest

from meshtastic.test import _FallbackDotMap


@pytest.mark.unit
def test_fallback_dotmap_autovivifies_missing_nested_keys() -> None:
    """Missing attribute chains should persist newly created child maps."""
    dmap = _FallbackDotMap()

    dmap.alpha.beta = 1

    assert isinstance(dmap["alpha"], _FallbackDotMap)
    assert dmap["alpha"]["beta"] == 1


@pytest.mark.unit
def test_fallback_dotmap_wraps_existing_dict_and_persists_wrapper() -> None:
    """Existing dict values should be wrapped once and stored back for subsequent writes."""
    dmap = _FallbackDotMap({"config": {"threshold": 5}})

    wrapped = dmap.config
    wrapped.enabled = True

    assert isinstance(wrapped, _FallbackDotMap)
    assert dmap["config"] is wrapped
    assert dmap["config"]["threshold"] == 5
    assert dmap["config"]["enabled"] is True
