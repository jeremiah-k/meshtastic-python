"""Settings request/response and admin command-family runtime owners."""

# pylint: disable=no-name-in-module
from meshtastic.node_runtime.settings_runtime.admin import _NodeAdminCommandRuntime
from meshtastic.node_runtime.settings_runtime.config_runtime import _NodeSettingsRuntime
from meshtastic.node_runtime.settings_runtime.message import _NodeSettingsMessageBuilder
from meshtastic.node_runtime.settings_runtime.owner import _NodeOwnerProfileRuntime
from meshtastic.node_runtime.settings_runtime.response import (
    _NodeSettingsResponseRuntime,
)
from meshtastic.util import toNodeNum

# Public export names (aliased from internal underscore names)
ConfigMessageBuilder = _NodeSettingsMessageBuilder
NodeSettingsRuntime = _NodeSettingsRuntime
NodeSettingsResponseRuntime = _NodeSettingsResponseRuntime
AdminCommandRuntime = _NodeAdminCommandRuntime
OwnerProfileRuntime = _NodeOwnerProfileRuntime

__all__ = [
    "ConfigMessageBuilder",
    "NodeSettingsRuntime",
    "NodeSettingsResponseRuntime",
    "AdminCommandRuntime",
    "OwnerProfileRuntime",
    # Private underscore names for internal use
    "_NodeSettingsMessageBuilder",
    "_NodeSettingsRuntime",
    "_NodeSettingsResponseRuntime",
    "_NodeAdminCommandRuntime",
    "_NodeOwnerProfileRuntime",
    # For compatibility with code mocking toNodeNum at this module level
    "toNodeNum",
]
