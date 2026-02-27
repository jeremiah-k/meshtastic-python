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


@pytest.mark.unit
def test_fallback_dotmap_autovivifies_three_levels() -> None:
    """Deep nested autovivification should persist intermediate maps."""
    dmap = _FallbackDotMap()

    dmap.a.b.c = 42

    assert isinstance(dmap["a"]["b"], _FallbackDotMap)
    assert dmap["a"]["b"]["c"] == 42


@pytest.mark.unit
def test_fallback_dotmap_dunder_guard_raises_attribute_error() -> None:
    """Dunder attribute access should be blocked to match safety expectations."""
    dmap = _FallbackDotMap()

    with pytest.raises(AttributeError, match="__foo__"):
        _ = dmap.__foo__


@pytest.mark.unit
def test_fallback_dotmap_delattr_and_missing_access() -> None:
    """Deleting a present key should remove it and missing delete should raise."""
    dmap = _FallbackDotMap()
    dmap.x = 1

    delattr(dmap, "x")

    with pytest.raises(AttributeError, match="x"):
        delattr(dmap, "x")


@pytest.mark.unit
def test_fallback_dotmap_scalar_readback() -> None:
    """Scalar values should be returned directly, not wrapped in maps."""
    dmap = _FallbackDotMap()
    dmap.x = 42

    assert dmap.x == 42
    assert not isinstance(dmap.x, _FallbackDotMap)


@pytest.mark.unit
def test_fallback_dotmap_dunder_setattr_delegates_to_object() -> None:
    """Dunder setattr should not create dictionary entries."""
    dmap = _FallbackDotMap()

    object.__setattr__(dmap, "__custom__", 1)

    assert "__custom__" not in dmap
