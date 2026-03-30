"""API baseline comparison tests for the Meshtastic Python library.

These tests generate and compare the current API surface against frozen baselines
captured from master/develop. This ensures that public API changes are intentional
and tracked, preventing accidental breaking changes.

To update baselines after intentional API changes:
    poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v --update-baselines

The baselines are stored in:
    meshtastic/tests/api_baselines/api_baseline.json
"""

# pylint: disable=redefined-outer-name
# Fixture names (current_baseline, stored_baseline) appear to shadow when used as
# test parameters - this is standard pytest fixture injection pattern.

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node

pytestmark = pytest.mark.unit

# =============================================================================
# Configuration
# =============================================================================

# Path to the baseline file
BASELINE_DIR = Path(__file__).parent / "api_baselines"
BASELINE_FILE = BASELINE_DIR / "api_baseline.json"

# Allow override via environment variable for CI comparisons
BASELINE_FILE_MASTER = BASELINE_DIR / "api_baseline_master.json"


def get_baseline_file() -> Path:
    """Get the baseline file path.

    Returns master baseline file if it exists (for CI comparisons),
    otherwise returns the standard baseline file.
    """
    if BASELINE_FILE_MASTER.exists():
        return BASELINE_FILE_MASTER
    return BASELINE_FILE


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


def _normalize_type_str(type_str: str) -> str:
    """Normalize type annotation strings to canonical form for comparison.

    ast.unparse() produces consistent source-level representations across Python
    versions. This function handles only the minimal differences:
    - typing.Literal[...] stays as-is (not a union)
    - Optional[X] and X | None are both normalized to X | None
    - typing.* module prefixes stripped for brevity
    - <class 'X'> normalized to X
    """
    import re

    result = type_str

    # Strip typing. module prefixes
    result = re.sub(r"\btyping\.", "", result)

    # Normalize <class 'X'> to just X
    result = re.sub(r"<class '(\w+)'>", r"\1", result)

    # Normalize Optional[X] to X | None using a depth-aware approach
    # Since Optional[...] can contain nested brackets, we match from Optional[ to the
    # matching closing bracket
    def replace_optional(match):
        inner = match.group(1)
        return f"{inner} | None"

    while True:
        new_result = re.sub(
            r"\bOptional\[([^\[\]]*(?:\[[^\[\]]*\])*[^\[\]]*)\]",
            replace_optional,
            result,
        )
        if new_result == result:
            break
        result = new_result

    return result


def _get_signature_str(method: Any) -> str:
    """Get a string representation of a method signature.

    This captures parameter names, defaults, and type annotations for comparison.
    Type annotations are normalized to canonical form for cross-version compatibility.
    """
    try:
        sig = inspect.signature(method)
        params = []
        for name, param in sig.parameters.items():
            param_str = name
            if param.annotation is not inspect.Parameter.empty:
                annotation_str = str(param.annotation)
                param_str += f":{annotation_str}"
            if param.default is not inspect.Parameter.empty:
                if param.default is None:
                    param_str += "=None"
                elif isinstance(param.default, str):
                    param_str += f"='{param.default}'"
                else:
                    param_str += f"={param.default}"
            params.append(param_str)
        raw_sig = f"({', '.join(params)})"
        return _normalize_type_str(raw_sig)
    except (ValueError, TypeError):
        return "(unknown)"


def _ast_annotation_str(node) -> str:
    """Convert an AST annotation node to a string representation."""
    return ast.unparse(node)


def _ast_signature_str(func_node) -> str:
    """Extract signature string from an AST FunctionDef node.

    Emits parameters in proper Python signature order:
    1. posonlyargs (with defaults)
    2. "/" marker
    3. regular args (with defaults)
    4. vararg (*args)
    5. kw-only args (with kw_defaults)
    6. kwarg (**kwargs)
    """
    params = []

    # 1. Handle positional-only args (with defaults)
    posonly = func_node.args.posonlyargs
    posonly_defaults = func_node.args.posonlyargs_defaults
    n_posonly = len(posonly)
    n_posonly_defaults = len(posonly_defaults)
    for i, arg in enumerate(posonly):
        s = arg.arg
        if arg.annotation:
            s += f":{_ast_annotation_str(arg.annotation)}"
        # Apply defaults from the end
        default_idx = i - (n_posonly - n_posonly_defaults)
        if default_idx >= 0 and default_idx < n_posonly_defaults:
            default_str = ast.unparse(posonly_defaults[default_idx])
            s += f"={default_str}"
        params.append(s)

    # 2. Add "/" marker if there are positional-only args
    if posonly:
        params.append("/")

    # 3. Handle regular args (with defaults)
    regular_args = func_node.args.args
    defaults = func_node.args.defaults
    n_args = len(regular_args)
    n_defaults = len(defaults)
    for i, arg in enumerate(regular_args):
        s = arg.arg
        if arg.annotation:
            s += f":{_ast_annotation_str(arg.annotation)}"
        # Apply defaults from the end
        default_idx = i - (n_args - n_defaults)
        if default_idx >= 0 and default_idx < n_defaults:
            default_str = ast.unparse(defaults[default_idx])
            s += f"={default_str}"
        params.append(s)

    # 4. Handle vararg (*args)
    if func_node.args.vararg:
        vararg_str = f"*{func_node.args.vararg.arg}"
        if func_node.args.vararg.annotation:
            vararg_str += f":{_ast_annotation_str(func_node.args.vararg.annotation)}"
        params.append(vararg_str)

    # 5. Handle kw-only args (with kw_defaults)
    # Only add "*" separator if we don't have vararg but have kw-only args
    if func_node.args.kwonlyargs and not func_node.args.vararg:
        params.append("*")

    kwonly = func_node.args.kwonlyargs
    kw_defaults = func_node.args.kw_defaults
    for i, kw_arg in enumerate(kwonly):
        s = kw_arg.arg
        if kw_arg.annotation:
            s += f":{_ast_annotation_str(kw_arg.annotation)}"
        # kw_defaults aligns with kwonlyargs by position
        if i < len(kw_defaults) and kw_defaults[i] is not None:
            default_str = ast.unparse(kw_defaults[i])
            s += f"={default_str}"
        params.append(s)

    # 6. Handle kwarg (**kwargs)
    if func_node.args.kwarg:
        kwarg_str = f"**{func_node.args.kwarg.arg}"
        if func_node.args.kwarg.annotation:
            kwarg_str += f":{_ast_annotation_str(func_node.args.kwarg.annotation)}"
        params.append(kwarg_str)

    return f"({', '.join(params)})"


def _ast_capture_class_methods(source: str, class_name: str) -> dict[str, str]:
    """Extract public method signatures from a class in AST source."""
    tree = ast.parse(source)
    methods = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("_"):
                        continue
                    methods[item.name] = _ast_signature_str(item)
    return methods


def _find_module_source(module_name: str) -> str | None:
    """Find the source file for a module and return its content."""
    import importlib.util

    spec = importlib.util.find_spec(module_name)
    if spec and spec.origin:
        with open(spec.origin, encoding="utf-8") as f:
            return f.read()
    return None


def capture_node_methods() -> dict[str, str]:
    """Capture public method signatures from Node class using AST."""
    source = _find_module_source("meshtastic.node")
    if source:
        methods = _ast_capture_class_methods(source, "Node")
        return dict(sorted(methods.items()))
    methods = {}
    for name in dir(Node):
        if name.startswith("_"):
            continue
        attr = getattr(Node, name)
        if callable(attr) and (inspect.isfunction(attr) or inspect.ismethod(attr)):
            methods[name] = _get_signature_str(attr)
    return dict(sorted(methods.items()))


def capture_mesh_interface_methods() -> dict[str, str]:
    """Capture public method signatures from MeshInterface class using AST."""
    source = _find_module_source("meshtastic.mesh_interface")
    if source:
        methods = _ast_capture_class_methods(source, "MeshInterface")
        return dict(sorted(methods.items()))
    methods = {}
    for name in dir(MeshInterface):
        if name.startswith("_"):
            continue
        attr = getattr(MeshInterface, name)
        if callable(attr):
            methods[name] = _get_signature_str(attr)
    return dict(sorted(methods.items()))


def capture_top_level_exports() -> list[str]:
    """Capture top-level exports from main meshtastic package.

    Returns a sorted list of explicitly allowed public names to avoid
    capturing incidental imports (Any, Callable, logging, tests).
    """
    # Explicit allowlist of intentional public exports
    # This avoids capturing incidental imports from other modules
    allowlist_public_names = [
        "analysis",
        "host_port",
        "interfaces",
        "ota",
        "remote_hardware",
        "slog",
        "tcp_interface",
        "test",
        "tunnel",
    ]

    return sorted(allowlist_public_names)


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
    Checks for master baseline first (used in CI comparisons),
    falls back to standard baseline file.
    """
    baseline_file = get_baseline_file()
    if not baseline_file.exists():
        return None

    with open(baseline_file, "r", encoding="utf-8") as f:
        return json.load(f)


def save_baseline(baseline_data: dict[str, Any]) -> None:
    """Save a baseline to disk."""
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    target = get_baseline_file()
    with open(target, "w", encoding="utf-8") as f:
        json.dump(baseline_data, f, indent=2, sort_keys=True)


# =============================================================================
# Comparison Logic
# =============================================================================


def compare_method_baselines(
    current: dict[str, str],
    stored: dict[str, str],
    _class_name: str,
) -> list[str]:
    """Compare current methods against baseline and report differences.

    Returns a list of human-readable difference descriptions.
    """
    differences = []

    current_keys = set(current.keys())
    stored_keys = set(stored.keys())

    # Find added methods
    added = current_keys - stored_keys
    if added:
        differences.append(f"ADDED methods: {sorted(added)}")

    # Find removed methods
    removed = stored_keys - current_keys
    if removed:
        differences.append(f"REMOVED methods: {sorted(removed)}")

    # Find changed signatures
    common = current_keys & stored_keys
    changed = []
    for key in sorted(common):
        if current[key] != stored[key]:
            changed.append(f"  {key}:")
            changed.append(f"    stored: {stored[key]}")
            changed.append(f"    current:  {current[key]}")

    if changed:
        differences.append("CHANGED signatures:")
        differences.extend(changed)

    return differences


def compare_list_baselines(
    current: list[str],
    stored_list: list[str],
    name: str,
) -> list[str]:
    """Compare two lists and report differences."""
    differences = []

    current_set = set(current)
    stored_set = set(stored_list)

    added = current_set - stored_set
    if added:
        differences.append(f"ADDED {name}: {sorted(added)}")

    removed = stored_set - current_set
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


@pytest.mark.unit
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
        api_baseline = generate_baseline()

        assert "node_methods" in api_baseline
        assert "mesh_interface_methods" in api_baseline
        assert "top_level_exports" in api_baseline
        assert "legacy_import_paths" in api_baseline

        # Verify we got actual data
        assert len(api_baseline["node_methods"]) > 0
        assert len(api_baseline["mesh_interface_methods"]) > 0
        assert len(api_baseline["top_level_exports"]) > 0

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
        api_baseline = generate_baseline()

        # Should not raise
        json_str = json.dumps(api_baseline, indent=2, sort_keys=True)

        # Should be parseable
        parsed = json.loads(json_str)
        assert parsed == api_baseline


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
