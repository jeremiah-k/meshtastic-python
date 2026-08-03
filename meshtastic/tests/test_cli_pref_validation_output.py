"""Regression tests for preference-validation CLI output routing."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from meshtastic import mt_config
from meshtastic.__main__ import _CONFIGURE_PREFLIGHT_MODE, setPref
from meshtastic.protobuf import localonly_pb2


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_direct_validation_error_remains_visible_in_quiet_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Quiet mode must not hide a direct user-facing validation failure."""
    mt_config.args = cast(Any, SimpleNamespace(quiet=True))
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.hop_limit", "bad") is False

    out, err = capsys.readouterr()
    assert "expected integer" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_configure_preflight_validation_respects_quiet_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Configure preflight may suppress its duplicate low-level diagnostic."""
    mt_config.args = cast(Any, SimpleNamespace(quiet=True))
    config = localonly_pb2.LocalConfig()
    token = _CONFIGURE_PREFLIGHT_MODE.set(True)
    try:
        assert setPref(config, "lora.hop_limit", "bad") is False
    finally:
        _CONFIGURE_PREFLIGHT_MODE.reset(token)

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
