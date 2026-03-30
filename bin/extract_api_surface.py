#!/usr/bin/env python3
"""Statically extract the public API surface from meshtastic source files.

Uses the ast module to parse Python source without importing anything.
Outputs a JSON baseline that can be diffed between branches.

Usage:
    python bin/extract_api_surface.py /path/to/meshtastic-package-dir
    python bin/extract_api_surface.py /path/to/meshtastic-package-dir --class MeshInterface
"""

import ast
import json
import sys
from pathlib import Path


def _annotation_to_str(node) -> str:
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
    if isinstance(node, ast.Index) and hasattr(node, "value"):
        return _annotation_to_str(node.value)
    if isinstance(node, ast.Starred):
        return f"*{_annotation_to_str(node.value)}"
    if isinstance(node, ast.List):
        return f"[{', '.join(_annotation_to_str(e) for e in node.elts)}]"
    return ast.dump(node)


def _default_to_str(node):
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


def _signature_from_function(func_node):
    params = []
    for arg in func_node.args.args:
        s = arg.arg
        ann = _annotation_to_str(arg.annotation) if arg.annotation else None
        if ann:
            s += f": {ann}"
        params.append(s)

    defaults = func_node.args.defaults
    n_args = len(func_node.args.args)
    n_defaults = len(defaults)
    for i, default_node in enumerate(defaults):
        arg_idx = n_args - n_defaults + i
        dv = _default_to_str(default_node)
        if dv is not None:
            params[arg_idx] += f"={dv}"

    if func_node.args.vararg:
        params.append(f"*{func_node.args.vararg.arg}")
    if func_node.args.kwonlyargs:
        if not func_node.args.vararg:
            params.append("*")
        for kw_arg in func_node.args.kwonlyargs:
            s = kw_arg.arg
            ann = _annotation_to_str(kw_arg.annotation) if kw_arg.annotation else None
            if ann:
                s += f": {ann}"
            params.append(s)
    if func_node.args.kwarg:
        params.append(f"**{func_node.args.kwarg.arg}")

    return f"({', '.join(params)})"


def _extract_class_methods(tree, class_name):
    methods = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("_"):
                        continue
                    methods[item.name] = _signature_from_function(item)
    return methods


def _find_source_file(pkg_dir, module_name):
    p = pkg_dir / f"{module_name}.py"
    if p.exists():
        return p
    p = pkg_dir / module_name / "__init__.py"
    if p.exists():
        return p
    return None


def _get_top_level_exports(pkg_dir):
    init_path = pkg_dir / "__init__.py"
    if not init_path.exists():
        return []
    tree = ast.parse(init_path.read_text(encoding="utf-8"))
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
                name = alias.asname if alias.asname else alias.name
                if not name.startswith("_"):
                    exports.add(name)
    return sorted(exports)


def extract_api_surface(pkg_dir, classes=None):
    pkg_dir = Path(pkg_dir)
    if classes is None:
        classes = ["MeshInterface", "Node"]

    module_map = {}
    for cls in classes:
        for candidate in ["mesh_interface", "node"]:
            src = _find_source_file(pkg_dir, candidate)
            if src is None:
                continue
            if src in module_map:
                continue
            tree = ast.parse(src.read_text(encoding="utf-8"))
            methods = _extract_class_methods(tree, cls)
            if methods:
                module_map[src] = (tree, candidate)
                break

    result = {
        "node_methods": {},
        "mesh_interface_methods": {},
        "top_level_exports": _get_top_level_exports(pkg_dir),
    }

    for cls in classes:
        key = f"{cls.lower().replace('meshinterface', 'mesh_interface')}_methods"
        if cls == "MeshInterface":
            key = "mesh_interface_methods"
        elif cls == "Node":
            key = "node_methods"
        for _src, (tree, _mod) in module_map.items():
            methods = _extract_class_methods(tree, cls)
            if methods:
                result[key] = dict(sorted(methods.items()))
                break

    return result


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-meshtastic-pkg-dir>", file=sys.stderr)
        sys.exit(1)

    pkg_dir = sys.argv[1]
    surface = extract_api_surface(pkg_dir)
    print(json.dumps(surface, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
