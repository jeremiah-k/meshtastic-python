"""Focused CLI bootstrap coverage for OTA node-loading policy."""

import argparse
from pathlib import Path
from unittest.mock import MagicMock, create_autospec

import pytest

from meshtastic.cli import bootstrap


@pytest.mark.unit
def test_ota_preflight_forces_no_nodes_before_transport_open(tmp_path: Path) -> None:
    """Verify OTA preflight disables NodeDB loading before transport open.

    Parameters
    ----------
    tmp_path : Path
        Temporary directory used to create a firmware image accepted by preflight.
    """
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    args = argparse.Namespace(
        quiet=False,
        debug=False,
        listen=False,
        debuglib=False,
        contact_verified=False,
        contact_ignore=False,
        contact_qr=None,
        configure=None,
        set_owner=None,
        set_owner_short=None,
        set_ham=None,
        ota_update=str(firmware),
        no_nodes=False,
        ch_index=None,
        dest=None,
        seriallog=None,
        noproto=False,
    )
    hooks = create_autospec(bootstrap.BootstrapHooks, instance=True)

    bootstrap._validate_and_normalize_args(args, MagicMock(), hooks)

    assert args.no_nodes is True
