#!/usr/bin/env python3
"""Statically extract the public API surface from meshtastic source files.

Uses the ast module to parse Python source without importing anything.
Outputs a JSON baseline that can be diffed between branches.

Usage:
    python bin/extract_api_surface.py /path/to/meshtastic-package-dir
"""

import ast
import json
import sys
from pathlib import Path
from typing import Any

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


def _annotation_to_str(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_annotation_to_str(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return f"{_annotation_to_str(node.value)}[{_annotation_to_str(node.slice)}]"
    if isinstance(node, ast.Tuple):
        return ", ".join(_annotation_to_str(e) for e in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{_annotation_to_str(node.left)} | {_annotation_to_str(node.right)}"
    if isinstance(node, ast.Starred):
        return f"*{_annotation_to_str(node.value)}"
    if isinstance(node, ast.List):
        return f"[{', '.join(_annotation_to_str(e) for e in node.elts)}]"
    return ast.dump(node)


def _default_to_str(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return repr(node.value)
        if node.value is None:
            return "None"
        return str(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _annotation_to_str(node)
    if isinstance(node, ast.List):
        items = [str(_default_to_str(e) or "") for e in node.elts]
        return f"[{', '.join(items)}]"
    if isinstance(node, ast.Tuple):
        items = [str(_default_to_str(e) or "") for e in node.elts]
        return f"({', '.join(items)})"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return f"-{_default_to_str(node.operand)}"
    if isinstance(node, ast.Call):
        return f"{_annotation_to_str(node.func)}(...)"
    return "..."


def _signature_from_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    params = []

    # Handle positional-only arguments
    for arg in func_node.args.posonlyargs:
        s = arg.arg
        ann = _annotation_to_str(arg.annotation) if arg.annotation else None
        if ann:
            s += f":{ann}"
        params.append(s)

    # Add "/" separator if positional-only args exist
    if func_node.args.posonlyargs:
        params.append("/")

    # Handle regular positional arguments
    for arg in func_node.args.args:
        s = arg.arg
        ann = _annotation_to_str(arg.annotation) if arg.annotation else None
        if ann:
            s += f":{ann}"
        params.append(s)

    # Calculate defaults - applies to last N args of combined posonlyargs + args
    posonlyargs = func_node.args.posonlyargs
    args = func_node.args.args
    defaults = func_node.args.defaults
    n_posonly = len(posonlyargs)
    n_args = len(args)
    n_defaults = len(defaults)
    has_posonly = bool(posonlyargs)

    # Defaults apply to the last n_defaults of combined (posonlyargs + args)
    # First apply defaults to posonlyargs (if any), then to args
    n_posonly_defaults = max(0, n_defaults - n_args)
    n_args_defaults = n_defaults - n_posonly_defaults

    # Apply defaults to posonlyargs
    for i in range(n_posonly_defaults):
        default_idx = i
        arg_idx = n_posonly - n_posonly_defaults + i
        dv = _default_to_str(defaults[default_idx])
        if dv is not None:
            params[arg_idx] += f"={dv}"

    # Apply defaults to regular args
    slash_offset = 1 if has_posonly else 0
    for i in range(n_args_defaults):
        default_idx = n_posonly_defaults + i
        arg_idx = n_posonly + slash_offset + n_args - n_args_defaults + i
        dv = _default_to_str(defaults[default_idx])
        if dv is not None:
            params[arg_idx] += f"={dv}"

    if func_node.args.vararg:
        params.append(f"*{func_node.args.vararg.arg}")
    if func_node.args.kwonlyargs:
        if not func_node.args.vararg:
            params.append("*")
        for kw_arg, kw_default in zip(
            func_node.args.kwonlyargs, func_node.args.kw_defaults, strict=False
        ):
            s = kw_arg.arg
            ann = _annotation_to_str(kw_arg.annotation) if kw_arg.annotation else None
            if ann:
                s += f":{ann}"
            dv = _default_to_str(kw_default)
            if dv is not None:
                s += f"={dv}"
            params.append(s)
    if func_node.args.kwarg:
        params.append(f"**{func_node.args.kwarg.arg}")

    return f"({', '.join(params)})"


def _extract_class_methods(tree: ast.AST, class_name: str) -> dict[str, str]:
    methods = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("_"):
                        continue
                    methods[item.name] = _signature_from_function(item)
    return methods


def _find_source_file(pkg_dir: Path, module_name: str) -> Path | None:
    p = pkg_dir / f"{module_name}.py"
    if p.exists():
        return p
    p = pkg_dir / module_name / "__init__.py"
    if p.exists():
        return p
    return None


def _get_top_level_exports(pkg_dir: Path) -> list[str]:
    init_path = pkg_dir / "__init__.py"
    if not init_path.exists():
        return []
    tree = ast.parse(init_path.read_text(encoding="utf-8"))

    # Check for __all__ first - if defined, it controls the public API
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        all_exports = [
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant)
                            and isinstance(elt.value, str)
                        ]
                        return sorted(all_exports)

    exports = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    exports.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not node.target.id.startswith("_"):
                exports.add(node.target.id)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            exports.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                exports.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.module.startswith("_"):
                for alias in node.names:
                    name = alias.asname if alias.asname else alias.name
                    if not name.startswith("_"):
                        exports.add(name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name.split(".", 1)[0]
                if not name.startswith("_"):
                    exports.add(name)

    # Historically importable top-level meshtastic modules/subpackages.
    # Keep this compatibility surface explicit and stable for baseline checks.
    exports.update(
        {
            "analysis",
            "host_port",
            "interfaces",
            "mesh_interface",
            "mesh_interface_runtime",
            "mt_config",
            "node",
            "node_runtime",
            "ota",
            "powermon",
            "protobuf",
            "remote_hardware",
            "serial_interface",
            "slog",
            "stream_interface",
            "supported_device",
            "tcp_interface",
            "tunnel",
            "util",
            "version",
        }
    )
    return sorted(exports)


def _module_path_exists(pkg_dir: Path, dotted_path: str) -> bool:
    """Return whether dotted module path exists under pkg_dir."""
    if not dotted_path.startswith("meshtastic."):
        return False
    relative_parts = dotted_path.split(".")[1:]
    module_py = pkg_dir.joinpath(*relative_parts).with_suffix(".py")
    if module_py.exists():
        return True
    module_init = pkg_dir.joinpath(*relative_parts, "__init__.py")
    return module_init.exists()


def _capture_legacy_import_paths(pkg_dir: Path) -> list[str]:
    """Capture compatibility import paths that exist in the provided tree."""
    return sorted(
        [path for path in LEGACY_IMPORT_PATHS if _module_path_exists(pkg_dir, path)]
    )


def extract_api_surface(
    pkg_dir: str | Path, classes: list[str] | None = None
) -> dict[str, Any]:
    pkg_dir = Path(pkg_dir)
    if classes is None:
        classes = ["MeshInterface", "Node"]

    module_map = {}
    for cls in classes:
        # Derive module name from class name using snake_case convention
        module_name = cls.lower().replace("meshinterface", "mesh_interface")
        src = _find_source_file(pkg_dir, module_name)
        if src is None:
            continue
        if src in module_map:
            continue
        tree = ast.parse(src.read_text(encoding="utf-8"))
        methods = _extract_class_methods(tree, cls)
        if methods:
            module_map[src] = (tree, module_name)

    result = {
        "node_methods": {},
        "mesh_interface_methods": {},
        "top_level_exports": _get_top_level_exports(pkg_dir),
        "legacy_import_paths": _capture_legacy_import_paths(pkg_dir),
    }

    for cls in classes:
        if cls == "MeshInterface":
            key = "mesh_interface_methods"
        elif cls == "Node":
            key = "node_methods"
        else:
            module_name = cls.lower().replace("meshinterface", "mesh_interface")
            key = f"{module_name}_methods"
        for tree, _mod in module_map.values():
            methods = _extract_class_methods(tree, cls)
            if methods:
                result[key] = dict(sorted(methods.items()))
                break

    return result


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-meshtastic-pkg-dir>", file=sys.stderr)
        sys.exit(1)

    pkg_dir = sys.argv[1]
    surface = extract_api_surface(pkg_dir)
    print(json.dumps(surface, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
