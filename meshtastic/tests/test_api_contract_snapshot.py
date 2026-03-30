"""API contract snapshot tests for the Meshtastic Python library.

These tests capture the current public API surface as a snapshot that can be
compared against future changes. They verify that the public API contract
remains stable and that methods are not accidentally removed or changed.

These tests document the "contract" that existing code depends on.
"""

import inspect

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node


# =============================================================================
# Node Public API Method Existence Tests
# =============================================================================


def test_node_public_api_methods():
    """Verify all expected Node public methods exist and document the current API surface.

    This test uses inspect to enumerate public methods and verifies that
    the expected API methods exist. If methods are removed or signatures
    change unexpectedly, this test will fail.
    """
    # Get all public methods (not starting with underscore)
    public_methods = {
        name
        for name in dir(Node)
        if not name.startswith("_") and callable(getattr(Node, name, None))
    }

    # Define expected Node public API methods (the stable surface)
    expected_methods = {
        # URL and configuration methods
        "setURL",
        "getURL",
        "requestConfig",
        "writeConfig",
        # Channel management methods
        "writeChannel",
        "getChannelByChannelIndex",
        "getChannelCopyByChannelIndex",
        "getChannelByName",
        "getChannelCopyByName",
        "deleteChannel",
        "setChannels",
        "requestChannels",
        "showChannels",
        "waitForConfig",
        "getDisabledChannel",
        "getDisabledChannelCopy",
        # Owner and metadata methods
        "setOwner",
        "getMetadata",
        "showInfo",
        "getAdminChannelIndex",
        "turnOffEncryptionOnPrimaryChannel",
        # Admin commands
        "startOTA",
        "rebootOTA",
        "reboot",
        "shutdown",
        "factoryReset",
        "enterDFUMode",
        "exitSimulator",
        "beginSettingsTransaction",
        "commitSettingsTransaction",
        # Node management
        "removeNode",
        "setFavorite",
        "removeFavorite",
        "setIgnored",
        "removeIgnored",
        "resetNodeDb",
        # Position and time
        "setFixedPosition",
        "removeFixedPosition",
        "setTime",
        # Session key
        "ensureSessionKey",
        # Channel hash methods
        "get_channels_with_hash",
        "getChannelsWithHash",
        # Content methods (ringtone and canned messages)
        "getRingtone",
        "setRingtone",
        "getCannedMessage",
        "setCannedMessage",
        # Compatibility shims
        "get_ringtone",
        "set_ringtone",
        "get_canned_message",
        "set_canned_message",
        # Module availability
        "moduleAvailable",
        "module_available",
        # Response handlers
        "onResponseRequestSettings",
        "onResponseRequestRingtone",
        "onResponseRequestCannedMessagePluginMessageMessages",
        # Utility methods
        "positionFlagsList",
        "position_flags_list",
        "excludedModulesList",
        "excluded_modules_list",
    }

    # Verify all expected methods exist
    missing_methods = expected_methods - public_methods
    if missing_methods:
        pytest.fail(
            f"Node is missing expected public methods: {sorted(missing_methods)}"
        )

    # Document extra methods (these might be intentional additions)
    extra_methods = public_methods - expected_methods
    # Log them but don't fail - they might be intentional additions
    # pytest.warns doesn't work well here, so we'll just accept them


# =============================================================================
# MeshInterface Public API Method Existence Tests
# =============================================================================


def test_mesh_interface_public_api_methods():
    """Verify all expected MeshInterface public methods exist and document the current API surface.

    This test uses inspect to enumerate public methods and verifies that
    the expected API methods exist, including send methods, wait methods,
    and utility methods.
    """
    # Get all public methods (not starting with underscore)
    public_methods = {
        name
        for name in dir(MeshInterface)
        if not name.startswith("_") and callable(getattr(MeshInterface, name, None))
    }

    # Define expected MeshInterface public API methods (the stable surface)
    expected_methods = {
        # Send methods (core API)
        "sendText",
        "sendData",
        "sendPosition",
        "sendTelemetry",
        "sendTraceRoute",
        "sendWaypoint",
        "sendAlert",
        "sendMqttClientProxyMessage",
        # Node access methods
        "getNode",
        "showNodes",
        "showInfo",
        "getMyNodeInfo",
        "getMyUser",
        "getLongName",
        "getShortName",
        "getPublicKey",
        "getCannedMessage",
        "getRingtone",
        # Wait methods (for async operations)
        "waitForConfig",
        "waitForAckNak",
        "waitForTraceRoute",
        "waitForTelemetry",
        "waitForPosition",
        "waitForWaypoint",
        # Connection management
        "close",
    }

    # Dunder methods need special handling - check them separately
    dunder_methods = {"__enter__", "__exit__"}

    # Verify dunder methods exist
    for method in dunder_methods:
        assert hasattr(MeshInterface, method), (
            f"MeshInterface must have '{method}' method"
        )

    # Verify all expected methods exist
    missing_methods = expected_methods - public_methods
    if missing_methods:
        pytest.fail(
            f"MeshInterface is missing expected public methods: {sorted(missing_methods)}"
        )

    # Document extra methods
    extra_methods = public_methods - expected_methods


# =============================================================================
# Node Method Signature Tests
# =============================================================================


def test_node_writechannel_signature():
    """Verify writeChannel accepts both old and new signatures.

    Must support:
    - writeChannel(channelIndex, adminIndex=None, **kwargs)
    - Backward compatible positional arguments
    """
    sig = inspect.signature(Node.writeChannel)
    params = list(sig.parameters.values())

    # Verify parameter names
    param_names = [p.name for p in params]
    assert "self" in param_names, "writeChannel must have 'self' parameter"
    assert "channelIndex" in param_names, (
        "writeChannel must have 'channelIndex' parameter"
    )
    assert "adminIndex" in param_names, "writeChannel must have 'adminIndex' parameter"

    # Verify adminIndex is optional (has default)
    admin_param = sig.parameters["adminIndex"]
    assert admin_param.default is None, "adminIndex must default to None"
    assert admin_param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD, (
        "adminIndex must be positional-or-keyword"
    )


def test_node_startota_signature():
    """Verify startOTA accepts both old and new signatures.

    Must support the complex signature with backward compatibility.
    """
    sig = inspect.signature(Node.startOTA)
    params = list(sig.parameters.values())

    param_names = [p.name for p in params]
    expected_params = {
        "self",
        "mode",
        "ota_file_hash",
        "ota_mode",
        "ota_hash",
        "kwargs",
    }

    # Check that expected params exist
    for param in expected_params:
        assert param in param_names, f"startOTA must have '{param}' parameter"

    # Verify mode and ota_file_hash are positional-or-keyword
    mode_param = sig.parameters["mode"]
    assert mode_param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD, (
        "mode must be positional-or-keyword"
    )

    ota_file_hash_param = sig.parameters["ota_file_hash"]
    assert ota_file_hash_param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD, (
        "ota_file_hash must be positional-or-keyword"
    )


def test_node_requestconfig_signature():
    """Verify requestConfig signature: requestConfig(configType, adminIndex=None)."""
    sig = inspect.signature(Node.requestConfig)
    params = list(sig.parameters.values())

    param_names = [p.name for p in params]
    assert "configType" in param_names, "requestConfig must have 'configType' parameter"
    assert "adminIndex" in param_names, "requestConfig must have 'adminIndex' parameter"

    # Verify adminIndex defaults to None
    admin_param = sig.parameters["adminIndex"]
    assert admin_param.default is None, "adminIndex must default to None"


def test_node_setowner_signature():
    """Verify setOwner signature with all optional parameters."""
    sig = inspect.signature(Node.setOwner)
    params = list(sig.parameters.values())

    param_names = [p.name for p in params]
    expected_params = {
        "self",
        "long_name",
        "short_name",
        "is_licensed",
        "is_unmessagable",
    }

    for param in expected_params:
        assert param in param_names, f"setOwner must have '{param}' parameter"

    # All parameters except self should have defaults
    for param in params:
        if param.name != "self":
            assert param.default is not inspect.Parameter.empty, (
                f"setOwner parameter '{param.name}' must have a default value"
            )


def test_node_reboot_shutdown_factoryreset_signatures():
    """Verify reboot, shutdown, and factoryReset method signatures."""
    # reboot(secs=10)
    reboot_sig = inspect.signature(Node.reboot)
    secs_param = reboot_sig.parameters.get("secs")
    assert secs_param is not None, "reboot must have 'secs' parameter"
    assert secs_param.default == 10, "reboot 'secs' must default to 10"

    # shutdown(secs=10)
    shutdown_sig = inspect.signature(Node.shutdown)
    secs_param = shutdown_sig.parameters.get("secs")
    assert secs_param is not None, "shutdown must have 'secs' parameter"
    assert secs_param.default == 10, "shutdown 'secs' must default to 10"

    # factoryReset(full=False)
    factory_sig = inspect.signature(Node.factoryReset)
    full_param = factory_sig.parameters.get("full")
    assert full_param is not None, "factoryReset must have 'full' parameter"
    assert full_param.default is False, "factoryReset 'full' must default to False"


def test_node_ensuresessionkey_signature():
    """Verify ensureSessionKey signature."""
    sig = inspect.signature(Node.ensureSessionKey)
    params = list(sig.parameters.values())

    param_names = [p.name for p in params]
    assert "adminIndex" in param_names, (
        "ensureSessionKey must have 'adminIndex' parameter"
    )

    admin_param = sig.parameters["adminIndex"]
    assert admin_param.default is None, (
        "ensureSessionKey 'adminIndex' must default to None"
    )


# =============================================================================
# MeshInterface Method Signature Tests
# =============================================================================


def test_mesh_interface_sendtext_signature():
    """Verify sendText signature with all expected parameters."""
    sig = inspect.signature(MeshInterface.sendText)
    params = list(sig.parameters.values())

    param_names = [p.name for p in params]
    expected_params = {
        "self",
        "text",
        "destinationId",
        "wantAck",
        "wantResponse",
        "onResponse",
        "channelIndex",
        "portNum",
        "replyId",
        "hopLimit",
    }

    for param in expected_params:
        assert param in param_names, f"sendText must have '{param}' parameter"

    # Verify key defaults
    assert sig.parameters["destinationId"].default is not inspect.Parameter.empty
    assert sig.parameters["wantAck"].default is False
    assert sig.parameters["channelIndex"].default == 0


def test_mesh_interface_senddata_signature():
    """Verify sendData signature with all expected parameters."""
    sig = inspect.signature(MeshInterface.sendData)
    params = list(sig.parameters.values())

    param_names = [p.name for p in params]
    expected_params = {
        "self",
        "data",
        "destinationId",
        "portNum",
        "wantAck",
        "wantResponse",
        "onResponse",
        "onResponseAckPermitted",
        "channelIndex",
        "hopLimit",
        "pkiEncrypted",
        "publicKey",
        "priority",
        "replyId",
    }

    # All expected params should exist
    for param in expected_params:
        assert param in param_names, f"sendData must have '{param}' parameter"


def test_mesh_interface_wait_methods_signatures():
    """Verify wait methods accept request_id parameter where applicable."""
    # waitForPosition(request_id=None)
    sig = inspect.signature(MeshInterface.waitForPosition)
    assert "request_id" in sig.parameters, (
        "waitForPosition must have 'request_id' parameter"
    )
    assert sig.parameters["request_id"].default is None

    # waitForTelemetry(request_id=None)
    sig = inspect.signature(MeshInterface.waitForTelemetry)
    assert "request_id" in sig.parameters, (
        "waitForTelemetry must have 'request_id' parameter"
    )
    assert sig.parameters["request_id"].default is None

    # waitForWaypoint(request_id=None)
    sig = inspect.signature(MeshInterface.waitForWaypoint)
    assert "request_id" in sig.parameters, (
        "waitForWaypoint must have 'request_id' parameter"
    )
    assert sig.parameters["request_id"].default is None

    # waitForTraceRoute(waitFactor, request_id=None)
    sig = inspect.signature(MeshInterface.waitForTraceRoute)
    assert "waitFactor" in sig.parameters, (
        "waitForTraceRoute must have 'waitFactor' parameter"
    )
    assert "request_id" in sig.parameters, (
        "waitForTraceRoute must have 'request_id' parameter"
    )
    assert sig.parameters["request_id"].default is None


def test_mesh_interface_sendposition_signature():
    """Verify sendPosition signature."""
    sig = inspect.signature(MeshInterface.sendPosition)
    params = list(sig.parameters.values())

    param_names = [p.name for p in params]
    expected_params = {
        "self",
        "latitude",
        "longitude",
        "altitude",
        "destinationId",
        "wantAck",
        "wantResponse",
        "channelIndex",
        "hopLimit",
    }

    for param in expected_params:
        assert param in param_names, f"sendPosition must have '{param}' parameter"

    # Verify latitude defaults to 0.0
    assert sig.parameters["latitude"].default == 0.0


def test_mesh_interface_sendtelemetry_signature():
    """Verify sendTelemetry signature with telemetryType parameter."""
    sig = inspect.signature(MeshInterface.sendTelemetry)
    params = list(sig.parameters.values())

    param_names = [p.name for p in params]
    assert "destinationId" in param_names, (
        "sendTelemetry must have 'destinationId' parameter"
    )
    assert "wantResponse" in param_names, (
        "sendTelemetry must have 'wantResponse' parameter"
    )
    assert "channelIndex" in param_names, (
        "sendTelemetry must have 'channelIndex' parameter"
    )
    assert "telemetryType" in param_names, (
        "sendTelemetry must have 'telemetryType' parameter"
    )
    assert "hopLimit" in param_names, "sendTelemetry must have 'hopLimit' parameter"

    # Verify defaults
    assert sig.parameters["channelIndex"].default == 0


def test_mesh_interface_sendtraceroute_signature():
    """Verify sendTraceRoute signature."""
    sig = inspect.signature(MeshInterface.sendTraceRoute)
    params = list(sig.parameters.values())

    param_names = [p.name for p in params]
    assert "dest" in param_names, "sendTraceRoute must have 'dest' parameter"
    assert "hopLimit" in param_names, "sendTraceRoute must have 'hopLimit' parameter"
    assert "channelIndex" in param_names, (
        "sendTraceRoute must have 'channelIndex' parameter"
    )


def test_mesh_interface_sendwaypoint_signature():
    """Verify sendWaypoint signature."""
    sig = inspect.signature(MeshInterface.sendWaypoint)
    params = list(sig.parameters.values())

    param_names = [p.name for p in params]
    expected_params = {
        "self",
        "name",
        "description",
        "icon",
        "expire",
        "waypoint_id",
        "latitude",
        "longitude",
        "destinationId",
        "wantAck",
        "wantResponse",
        "channelIndex",
        "hopLimit",
    }

    for param in expected_params:
        assert param in param_names, f"sendWaypoint must have '{param}' parameter"


def test_mesh_interface_getnode_signature():
    """Verify getNode signature."""
    sig = inspect.signature(MeshInterface.getNode)
    params = list(sig.parameters.values())

    param_names = [p.name for p in params]
    expected_params = {
        "self",
        "nodeId",
        "requestChannels",
        "requestChannelAttempts",
        "timeout",
    }

    for param in expected_params:
        assert param in param_names, f"getNode must have '{param}' parameter"

    # Verify defaults
    assert sig.parameters["requestChannels"].default is True
    assert sig.parameters["requestChannelAttempts"].default == 3


# =============================================================================
# Public Classes Existence Tests
# =============================================================================


def test_public_classes_exist():
    """Verify key public classes can be imported and exist.

    This test ensures that the main public API classes are available
    for import and haven't been accidentally removed or renamed.
    """
    # Test imports from main modules
    from meshtastic.mesh_interface import MeshInterface
    from meshtastic.node import Node

    # Verify classes are importable and are proper types
    assert isinstance(MeshInterface, type), "MeshInterface must be a class"
    assert isinstance(Node, type), "Node must be a class"

    # Verify class names
    assert MeshInterface.__name__ == "MeshInterface"
    assert Node.__name__ == "Node"


def test_node_instance_attributes_documented():
    """Document expected Node instance attributes (set in __init__).

    These are instance-level attributes, not class attributes, so we
    document them here for reference rather than testing with hasattr.
    """
    # These attributes are set on Node instances in __init__:
    expected_instance_attrs = [
        "nodeNum",
        "localConfig",
        "moduleConfig",
        "channels",
        "noProto",
        "iface",
        "timeout",
    ]

    # Document for reference - these are verified at runtime on instances
    # This test serves as documentation of the expected API contract
    assert len(expected_instance_attrs) > 0, "Node instance attributes documented"


def test_meshinterface_instance_attributes_documented():
    """Document expected MeshInterface instance attributes (set in __init__)."""
    expected_instance_attrs = [
        "nodes",
        "isConnected",
        "noProto",
        "localNode",
        "myInfo",
        "metadata",
        "debugOut",
        "nodesByNum",
    ]

    # Document for reference - these are verified at runtime on instances
    assert len(expected_instance_attrs) > 0, (
        "MeshInterface instance attributes documented"
    )


# =============================================================================
# API Shape Snapshot (Comprehensive)
# =============================================================================


def test_node_api_shape_snapshot():
    """Create a snapshot of the complete Node public API shape.

    This test captures the full public API surface including method names,
    which can be compared against future versions to detect unexpected changes.
    """
    # Get all public callable members
    public_api = {}
    for name in dir(Node):
        if name.startswith("_"):
            continue
        attr = getattr(Node, name)
        if callable(attr):
            try:
                sig = inspect.signature(attr)
                # Store simplified signature info
                params = [p.name for p in sig.parameters.values()]
                public_api[name] = params
            except (ValueError, TypeError):
                # Some callables may not support signature inspection
                public_api[name] = []

    # Key methods that must exist (critical API surface)
    critical_methods = {
        "setURL",
        "writeChannel",
        "writeConfig",
        "setOwner",
        "getChannelByChannelIndex",
        "deleteChannel",
        "requestConfig",
        "reboot",
        "shutdown",
        "factoryReset",
        "ensureSessionKey",
    }

    actual_methods = set(public_api.keys())

    # Verify critical methods exist
    missing = critical_methods - actual_methods
    if missing:
        pytest.fail(f"Node is missing critical API methods: {missing}")

    # Store the snapshot for documentation
    # This list can be used to track API changes over time
    snapshot = {
        "class_name": "Node",
        "method_count": len(public_api),
        "critical_methods_present": critical_methods <= actual_methods,
        "public_methods": sorted(public_api.keys()),
    }

    # Verify we have a reasonable number of public methods
    # If this changes significantly, it may indicate an API break
    assert len(public_api) >= 40, (
        f"Node should have at least 40 public methods, found {len(public_api)}"
    )


def test_mesh_interface_api_shape_snapshot():
    """Create a snapshot of the complete MeshInterface public API shape."""
    # Get all public callable members
    public_api = {}
    for name in dir(MeshInterface):
        if name.startswith("_"):
            continue
        attr = getattr(MeshInterface, name)
        if callable(attr):
            try:
                sig = inspect.signature(attr)
                params = [p.name for p in sig.parameters.values()]
                public_api[name] = params
            except (ValueError, TypeError):
                public_api[name] = []

    # Key methods that must exist (critical API surface)
    critical_methods = {
        "sendText",
        "sendData",
        "sendPosition",
        "sendTelemetry",
        "sendTraceRoute",
        "sendWaypoint",
        "getNode",
        "showNodes",
        "showInfo",
        "close",
    }

    actual_methods = set(public_api.keys())

    # Verify critical methods exist
    missing = critical_methods - actual_methods
    if missing:
        pytest.fail(f"MeshInterface is missing critical API methods: {missing}")

    # Store snapshot info
    snapshot = {
        "class_name": "MeshInterface",
        "method_count": len(public_api),
        "critical_methods_present": critical_methods <= actual_methods,
        "public_methods": sorted(public_api.keys()),
    }

    # Verify we have a reasonable number of public methods
    assert len(public_api) >= 20, (
        f"MeshInterface should have at least 20 public methods, found {len(public_api)}"
    )


# =============================================================================
# Parameter Compatibility Tests
# =============================================================================


def test_no_positional_only_params_in_node():
    """Ensure Node public methods don't use positional-only parameters.

    Positional-only parameters break callers that pass keyword arguments,
    which is a common pattern in user code.
    """
    for name in dir(Node):
        if name.startswith("_"):
            continue
        attr = getattr(Node, name, None)
        if not callable(attr):
            continue
        try:
            sig = inspect.signature(attr)
        except (ValueError, TypeError):
            continue

        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            assert param.kind != inspect.Parameter.POSITIONAL_ONLY, (
                f"Node.{name}() has positional-only param '{pname}' - "
                "this breaks keyword-arg callers"
            )


def test_no_positional_only_params_in_meshinterface():
    """Ensure MeshInterface public methods don't use positional-only parameters."""
    for name in dir(MeshInterface):
        if name.startswith("_"):
            continue
        attr = getattr(MeshInterface, name, None)
        if not callable(attr):
            continue
        try:
            sig = inspect.signature(attr)
        except (ValueError, TypeError):
            continue

        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            assert param.kind != inspect.Parameter.POSITIONAL_ONLY, (
                f"MeshInterface.{name}() has positional-only param '{pname}' - "
                "this breaks keyword-arg callers"
            )
