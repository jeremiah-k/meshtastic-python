"""API baseline comparison tests for the Meshtastic Python library.

These tests generate and compare the current API surface against frozen baselines
captured from master/develop. This ensures that public API changes are intentional
and tracked, preventing accidental breaking changes.

To update baselines after intentional API changes:
    poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v --update-baselines

The baselines are stored in:
    meshtastic/tests/api_baselines/api_baseline.json
"""

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node

# =============================================================================
# Configuration
# =============================================================================

# Path to the baseline file
BASELINE_DIR = Path(__file__).parent / "api_baselines"
BASELINE_FILE = BASELINE_DIR / "api_baseline.json"

# Legacy import paths to verify
LEGACY_IMPORT_PATHS = [
    "meshtastic.node_runtime.settings_runtime",
    "meshtastic.node_runtime.channel_request_runtime",
    "meshtastic.node_runtime.channel_lookup_runtime",
    "meshtastic.node_runtime.channel_export_runtime",
    "meshtastic.node_runtime.channel_presentation_runtime",
    "meshtastic.node_runtime.channel_normalization_runtime",
    "meshtastic.node_runtime.seturl_runtime",
    "meshtastic.node_runtime.transport_runtime",
    "meshtastic.node_runtime.content_runtime",
    "meshtastic.node_runtime.shared",
]


# =============================================================================
# Baseline Generation Utilities
# =============================================================================


def _get_signature_str(method: Any) -> str:
    """Get a string representation of a method signature.

    This captures parameter names, defaults, and type annotations for comparison.
    """
    try:
        sig = inspect.signature(method)
        params = []
        for name, param in sig.parameters.items():
            param_str = name
            if param.annotation is not inspect.Parameter.empty:
                param_str += f":{param.annotation}"
            if param.default is not inspect.Parameter.empty:
                if param.default is None:
                    param_str += "=None"
                elif isinstance(param.default, str):
                    param_str += f"='{param.default}'"
                else:
                    param_str += f"={param.default}"
            params.append(param_str)
        return f"({', '.join(params)})"
    except (ValueError, TypeError):
        return "(unknown)"


def capture_node_methods() -> dict[str, str]:
    """Capture public method signatures from Node class.

    Returns a dictionary mapping method names to their signature strings.
    """
    methods = {}
    for name in dir(Node):
        if name.startswith("_"):
            continue
        attr = getattr(Node, name)
        if callable(attr) and inspect.isfunction(attr) or inspect.ismethod(attr):
            methods[name] = _get_signature_str(attr)
    return methods


def capture_mesh_interface_methods() -> dict[str, str]:
    """Capture public method signatures from MeshInterface class.

    Returns a dictionary mapping method names to their signature strings.
    """
    methods = {}
    for name in dir(MeshInterface):
        if name.startswith("_"):
            continue
        attr = getattr(MeshInterface, name)
        if callable(attr):
            methods[name] = _get_signature_str(attr)
    return methods


def capture_top_level_exports() -> list[str]:
    """Capture top-level exports from main meshtastic package.

    Returns a sorted list of exported names that don't start with underscore.
    """
    import meshtastic

    exports = []
    for name in dir(meshtastic):
        if name.startswith("_"):
            continue
        exports.append(name)
    return sorted(exports)


def capture_legacy_import_paths() -> list[str]:
    """Verify legacy import paths still work.

    Attempts to import each legacy path and records which succeed.
    Returns a sorted list of working import paths.
    """
    working_paths = []
    for path in LEGACY_IMPORT_PATHS:
        try:
            __import__(path)
            working_paths.append(path)
        except ImportError:
            # Skip paths that don't exist (they may have been removed intentionally)
            pass
    return sorted(working_paths)


def generate_baseline() -> dict[str, Any]:
    """Generate a complete API baseline from current code.

    Returns a dictionary containing all captured API surfaces.
    """
    return {
        "node_methods": capture_node_methods(),
        "mesh_interface_methods": capture_mesh_interface_methods(),
        "top_level_exports": capture_top_level_exports(),
        "legacy_import_paths": capture_legacy_import_paths(),
    }


# =============================================================================
# Baseline Persistence
# =============================================================================


def load_baseline() -> dict[str, Any] | None:
    """Load the stored baseline from disk.

    Returns None if no baseline exists.
    """
    if not BASELINE_FILE.exists():
        return None

    with open(BASELINE_FILE, "r") as f:
        return json.load(f)


def save_baseline(baseline: dict[str, Any]) -> None:
    """Save a baseline to disk."""
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_FILE, "w") as f:
        json.dump(baseline, f, indent=2, sort_keys=True)


# =============================================================================
# Comparison Logic
# =============================================================================


def compare_method_baselines(
    current: dict[str, str],
    baseline: dict[str, str],
    class_name: str,
) -> list[str]:
    """Compare current methods against baseline and report differences.

    Returns a list of human-readable difference descriptions.
    """
    differences = []

    current_keys = set(current.keys())
    baseline_keys = set(baseline.keys())

    # Find added methods
    added = current_keys - baseline_keys
    if added:
        differences.append(f"ADDED methods: {sorted(added)}")

    # Find removed methods
    removed = baseline_keys - current_keys
    if removed:
        differences.append(f"REMOVED methods: {sorted(removed)}")

    # Find changed signatures
    common = current_keys & baseline_keys
    changed = []
    for key in sorted(common):
        if current[key] != baseline[key]:
            changed.append(f"  {key}:")
            changed.append(f"    baseline: {baseline[key]}")
            changed.append(f"    current:  {current[key]}")

    if changed:
        differences.append("CHANGED signatures:")
        differences.extend(changed)

    return differences


def compare_list_baselines(
    current: list[str],
    baseline: list[str],
    name: str,
) -> list[str]:
    """Compare two lists and report differences."""
    differences = []

    current_set = set(current)
    baseline_set = set(baseline)

    added = current_set - baseline_set
    if added:
        differences.append(f"ADDED {name}: {sorted(added)}")

    removed = baseline_set - current_set
    if removed:
        differences.append(f"REMOVED {name}: {sorted(removed)}")

    return differences


# =============================================================================
# Pytest Fixtures and Hooks
# =============================================================================


@pytest.fixture
def current_baseline():
    """Fixture providing the current API baseline."""
    return generate_baseline()


@pytest.fixture
def stored_baseline():
    """Fixture providing the stored baseline, or None if not exists."""
    return load_baseline()


def _should_update_baselines(pytestconfig):
    """Safely check if --update-baselines flag is set."""
    try:
        return pytestconfig.getoption("--update-baselines", default=False)
    except (ValueError, AttributeError):
        return False


# =============================================================================
# Test Cases
# =============================================================================


class TestNodeAPIAgainstBaseline:
    """Tests to verify Node public API matches baseline."""

    def test_node_api_against_baseline(
        self, current_baseline, stored_baseline, pytestconfig
    ):
        """Verify Node public API methods match the stored baseline.

        This test fails if:
        - Methods are added (new API surface)
        - Methods are removed (breaking change)
        - Method signatures change (breaking change)

        Use --update-baselines to accept intentional changes.
        """
        # Generate current baseline if none exists
        if stored_baseline is None:
            if _should_update_baselines(pytestconfig):
                save_baseline(current_baseline)
                pytest.skip("Created initial baseline - re-run tests")
            else:
                pytest.fail(
                    "No baseline exists. Run with --update-baselines to create one."
                )

        # Compare current against stored
        differences = compare_method_baselines(
            current_baseline["node_methods"],
            stored_baseline.get("node_methods", {}),
            "Node",
        )

        if _should_update_baselines(pytestconfig):
            # Update baseline with current state
            stored_baseline["node_methods"] = current_baseline["node_methods"]
            save_baseline(stored_baseline)
            pytest.skip("Updated baseline with current Node API")

        if differences:
            msg = "Node API differs from baseline:\n" + "\n".join(
                f"  {d}" for d in differences
            )
            msg += "\n\nTo accept these changes, run: poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v --update-baselines"
            pytest.fail(msg)

    def test_node_critical_methods_present(self, current_baseline):
        """Verify critical Node methods are always present regardless of baseline.

        This is a safety net to ensure core functionality isn't broken.
        """
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

        current_methods = set(current_baseline["node_methods"].keys())
        missing = critical_methods - current_methods

        if missing:
            pytest.fail(f"Critical Node methods missing: {sorted(missing)}")


class TestMeshInterfaceAPIAgainstBaseline:
    """Tests to verify MeshInterface public API matches baseline."""

    def test_mesh_interface_api_against_baseline(
        self, current_baseline, stored_baseline, pytestconfig
    ):
        """Verify MeshInterface public API methods match the stored baseline."""
        if stored_baseline is None:
            if _should_update_baselines(pytestconfig):
                save_baseline(current_baseline)
                pytest.skip("Created initial baseline - re-run tests")
            else:
                pytest.fail(
                    "No baseline exists. Run with --update-baselines to create one."
                )

        differences = compare_method_baselines(
            current_baseline["mesh_interface_methods"],
            stored_baseline.get("mesh_interface_methods", {}),
            "MeshInterface",
        )

        if _should_update_baselines(pytestconfig):
            stored_baseline["mesh_interface_methods"] = current_baseline[
                "mesh_interface_methods"
            ]
            save_baseline(stored_baseline)
            pytest.skip("Updated baseline with current MeshInterface API")

        if differences:
            msg = "MeshInterface API differs from baseline:\n" + "\n".join(
                f"  {d}" for d in differences
            )
            msg += "\n\nTo accept these changes, run: poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v --update-baselines"
            pytest.fail(msg)

    def test_mesh_interface_critical_methods_present(self, current_baseline):
        """Verify critical MeshInterface methods are always present."""
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

        current_methods = set(current_baseline["mesh_interface_methods"].keys())
        missing = critical_methods - current_methods

        if missing:
            pytest.fail(f"Critical MeshInterface methods missing: {sorted(missing)}")


class TestTopLevelExportsAgainstBaseline:
    """Tests to verify main package exports match baseline."""

    def test_top_level_exports_against_baseline(
        self, current_baseline, stored_baseline, pytestconfig
    ):
        """Verify top-level exports from meshtastic package match baseline."""
        if stored_baseline is None:
            if _should_update_baselines(pytestconfig):
                save_baseline(current_baseline)
                pytest.skip("Created initial baseline - re-run tests")
            else:
                pytest.fail(
                    "No baseline exists. Run with --update-baselines to create one."
                )

        differences = compare_list_baselines(
            current_baseline["top_level_exports"],
            stored_baseline.get("top_level_exports", []),
            "exports",
        )

        if _should_update_baselines(pytestconfig):
            stored_baseline["top_level_exports"] = current_baseline["top_level_exports"]
            save_baseline(stored_baseline)
            pytest.skip("Updated baseline with current top-level exports")

        if differences:
            msg = "Top-level exports differ from baseline:\n" + "\n".join(
                f"  {d}" for d in differences
            )
            msg += "\n\nTo accept these changes, run: poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v --update-baselines"
            pytest.fail(msg)

    def test_essential_exports_present(self, current_baseline):
        """Verify essential exports are always present."""
        essential_exports = {
            "Node",
            "BROADCAST_ADDR",
            "BROADCAST_NUM",
            "LOCAL_ADDR",
        }

        current_exports = set(current_baseline["top_level_exports"])
        missing = essential_exports - current_exports

        if missing:
            pytest.fail(f"Essential exports missing: {sorted(missing)}")


class TestLegacyImportPathsAgainstBaseline:
    """Tests to verify internal import paths still work."""

    def test_legacy_import_paths_against_baseline(
        self, current_baseline, stored_baseline, pytestconfig
    ):
        """Verify legacy internal import paths still work.

        This test ensures that code using internal imports doesn't break
        when the package structure changes.
        """
        if stored_baseline is None:
            if _should_update_baselines(pytestconfig):
                save_baseline(current_baseline)
                pytest.skip("Created initial baseline - re-run tests")
            else:
                pytest.fail(
                    "No baseline exists. Run with --update-baselines to create one."
                )

        differences = compare_list_baselines(
            current_baseline["legacy_import_paths"],
            stored_baseline.get("legacy_import_paths", []),
            "import paths",
        )

        if _should_update_baselines(pytestconfig):
            stored_baseline["legacy_import_paths"] = current_baseline[
                "legacy_import_paths"
            ]
            save_baseline(stored_baseline)
            pytest.skip("Updated baseline with current import paths")

        if differences:
            msg = "Legacy import paths differ from baseline:\n" + "\n".join(
                f"  {d}" for d in differences
            )
            msg += "\n\nTo accept these changes, run: poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v --update-baselines"
            pytest.fail(msg)


class TestBaselineGeneration:
    """Tests for baseline generation utilities."""

    def test_baseline_generation_succeeds(self):
        """Verify baseline generation works without errors."""
        baseline = generate_baseline()

        assert "node_methods" in baseline
        assert "mesh_interface_methods" in baseline
        assert "top_level_exports" in baseline
        assert "legacy_import_paths" in baseline

        # Verify we got actual data
        assert len(baseline["node_methods"]) > 0
        assert len(baseline["mesh_interface_methods"]) > 0
        assert len(baseline["top_level_exports"]) > 0

    def test_signature_capture_includes_parameters(self):
        """Verify signature capture includes parameter information."""
        methods = capture_node_methods()

        # Check a known method has signature info
        assert "setOwner" in methods
        sig = methods["setOwner"]
        # Should include parameter names
        assert "self" in sig
        assert "long_name" in sig

    def test_baseline_json_serializable(self):
        """Verify generated baseline can be serialized to JSON."""
        baseline = generate_baseline()

        # Should not raise
        json_str = json.dumps(baseline, indent=2, sort_keys=True)

        # Should be parseable
        parsed = json.loads(json_str)
        assert parsed == baseline


# =============================================================================
# Main Entry Point for Manual Baseline Generation
# =============================================================================

if __name__ == "__main__":
    # Allow running this file directly to generate baselines
    print("Generating API baseline...")
    baseline = generate_baseline()

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    save_baseline(baseline)

    print(f"Baseline saved to: {BASELINE_FILE}")
    print(f"Node methods: {len(baseline['node_methods'])}")
    print(f"MeshInterface methods: {len(baseline['mesh_interface_methods'])}")
    print(f"Top-level exports: {len(baseline['top_level_exports'])}")
    print(f"Legacy import paths: {len(baseline['legacy_import_paths'])}")
    print(
        "\nRun tests with: poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v"
    )
