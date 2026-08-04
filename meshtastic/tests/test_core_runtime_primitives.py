"""Compatibility and dependency-boundary tests for core runtime primitives."""

from __future__ import annotations

import ast
import inspect
import pickle
from pathlib import Path

import meshtastic
from meshtastic import _core_constants, _response_types


ROOT = Path(__file__).resolve().parents[2]


def _imports_from_meshtastic_root(path: Path) -> set[str]:
    """Return names imported directly from the package root by one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "meshtastic":
            imported.update(alias.name for alias in node.names)
    return imported


def test_public_core_constants_are_leaf_module_identities() -> None:
    """Package-root constants should remain exact re-exports of leaf primitives."""
    assert meshtastic.LOCAL_ADDR is _core_constants.LOCAL_ADDR
    assert meshtastic.BROADCAST_ADDR is _core_constants.BROADCAST_ADDR
    assert meshtastic.BROADCAST_NUM is _core_constants.BROADCAST_NUM
    assert meshtastic.OUR_APP_VERSION is _core_constants.OUR_APP_VERSION
    assert meshtastic.NODELESS_WANT_CONFIG_ID is _core_constants.NODELESS_WANT_CONFIG_ID
    assert meshtastic.DECODE_ERROR_KEY is _core_constants.DECODE_ERROR_KEY


def test_public_response_types_are_leaf_module_identities() -> None:
    """Historical response types should retain identity through the package facade."""
    assert meshtastic.ResponseCallback is _response_types.ResponseCallback
    assert meshtastic.ResponseHandler is _response_types.ResponseHandler


def test_core_runtime_consumers_do_not_import_moved_primitives_from_package_root() -> None:
    """Internal runtimes should depend on leaf primitives rather than package implementation."""
    moved_names = {
        "BROADCAST_ADDR",
        "BROADCAST_NUM",
        "DECODE_ERROR_KEY",
        "LOCAL_ADDR",
        "NODELESS_WANT_CONFIG_ID",
        "ResponseHandler",
    }
    paths = (
        ROOT / "meshtastic/mesh_interface.py",
        ROOT / "meshtastic/mesh_interface_runtime/flows.py",
        ROOT / "meshtastic/mesh_interface_runtime/node_view.py",
        ROOT / "meshtastic/mesh_interface_runtime/receive_pipeline.py",
        ROOT / "meshtastic/mesh_interface_runtime/request_wait.py",
        ROOT / "meshtastic/mesh_interface_runtime/send_pipeline.py",
    )
    for path in paths:
        assert _imports_from_meshtastic_root(path).isdisjoint(moved_names), path


def test_public_response_handler_metadata_and_pickle_contract_are_preserved() -> None:
    """Moving ResponseHandler must not change its historical public metadata."""
    assert meshtastic.ResponseHandler.__module__ == "meshtastic"
    response_handler = meshtastic.ResponseHandler(callback=str, ackPermitted=True)
    assert pickle.loads(pickle.dumps(response_handler)) == response_handler
    assert str(inspect.signature(meshtastic.ResponseHandler)) == (
        "(callback: Callable[[dict[str, Any]], Any], ackPermitted: bool = False)"
    )


def test_core_owner_modules_do_not_import_package_root() -> None:
    """Core leaf modules should stay below the package facade."""
    for path in (
        ROOT / "meshtastic/_core_constants.py",
        ROOT / "meshtastic/_response_types.py",
    ):
        assert _imports_from_meshtastic_root(path) == set(), path
