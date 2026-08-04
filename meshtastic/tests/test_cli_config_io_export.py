"""Regression tests for CLI configuration export normalization."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from meshtastic.cli.config_io import export_config
from meshtastic.protobuf import localonly_pb2


@pytest.mark.unit
def test_empty_config_mappings_export_firmware_true_defaults_as_false() -> None:
    """Empty protobuf mappings must still preserve firmware-true settings."""
    interface = MagicMock()
    interface.getLongName.return_value = None
    interface.getShortName.return_value = None
    interface.getMyNodeInfo.return_value = None
    interface.getCannedMessage.return_value = None
    interface.getRingtone.return_value = None
    interface.localNode = SimpleNamespace(
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
        getURL=lambda: None,
    )

    exported = yaml.safe_load(export_config(interface, camel_case=False))

    assert exported["config"]["bluetooth"]["enabled"] is False
    assert exported["config"]["lora"]["txEnabled"] is False
    assert exported["module_config"]["mqtt"]["encryptionEnabled"] is False
