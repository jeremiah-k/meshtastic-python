#!/usr/bin/env python3
"""Compare two API surface JSON files and report differences.

Used by CI to detect breaking API changes between master and PR branches.
Exits 0 if no removed methods/exports, exits 1 if breaking changes found.

Usage:
    python3 bin/compare_api_surfaces.py master_surface.json pr_surface.json
"""

import json
import re
import sys
from typing import Any


def _normalize_type(type_str: str) -> str:
    type_str = re.sub(r"\bUnion\[([^\[\]]+),\s*None\]", r"Optional[\1]", type_str)
    type_str = re.sub(r"\bOptional\[([^\[\]]+)\]", r"\1 | None", type_str)
    type_str = re.sub(r"typing\.", "", type_str)
    return type_str


def _find_top_level_char(text: str, target: str) -> int:
    """Return index of target at top level, or -1 if absent."""
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0
    for index, char in enumerate(text):
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren -= 1
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            depth_bracket -= 1
        elif char == "{":
            depth_brace += 1
        elif char == "}":
            depth_brace -= 1
        elif (
            char == target
            and depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
        ):
            return index
    return -1


def _split_top_level_params(sig: str) -> list[str]:
    """Split `(a, b: X)` into top-level parameter tokens."""
    trimmed = sig.strip()
    if trimmed.startswith("(") and trimmed.endswith(")"):
        trimmed = trimmed[1:-1]

    if not trimmed:
        return []

    params: list[str] = []
    start = 0
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0

    for index, char in enumerate(trimmed):
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren -= 1
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            depth_bracket -= 1
        elif char == "{":
            depth_brace += 1
        elif char == "}":
            depth_brace -= 1
        elif (
            char == "," and depth_paren == 0 and depth_bracket == 0 and depth_brace == 0
        ):
            params.append(trimmed[start:index].strip())
            start = index + 1

    params.append(trimmed[start:].strip())
    return params


def _normalize_param(param: str) -> str:
    """Normalize a single parameter token while preserving defaults."""
    if param in {"", "*", "/"}:
        return param

    colon_index = _find_top_level_char(param, ":")
    if colon_index == -1:
        return param

    name_part = param[:colon_index].strip()
    rest = param[colon_index + 1 :].strip()

    equals_index = _find_top_level_char(rest, "=")
    if equals_index == -1:
        type_part = rest
        default_part = None
    else:
        type_part = rest[:equals_index].strip()
        default_part = rest[equals_index + 1 :].strip()

    normalized = f"{name_part}: {_normalize_type(type_part)}"
    if default_part is not None:
        normalized += f"={default_part}"
    return normalized


def _normalize_sig(sig: str) -> str:
    """Normalize signature with top-level parameter parsing."""
    params = _split_top_level_params(sig)
    normalized = [_normalize_param(param) for param in params]
    return f"({', '.join(normalized)})"


def _parse_signature_shape(sig: str) -> list[dict[str, Any]]:
    """Parse signature into comparable parameter-shape records.

    Ignores annotation text and default values, but keeps whether a parameter
    is required and its call kind.
    """
    tokens = _split_top_level_params(sig)
    slash_index = tokens.index("/") if "/" in tokens else -1

    params: list[dict[str, Any]] = []
    kw_only_mode = False

    for index, token in enumerate(tokens):
        if not token:
            continue
        if token == "/":
            continue
        if token == "*":
            kw_only_mode = True
            continue

        raw = token.strip()
        if raw.startswith("**"):
            name_body = raw[2:].strip()
            kind = "var_keyword"
            required = False
        elif raw.startswith("*"):
            name_body = raw[1:].strip()
            kind = "var_positional"
            required = False
            kw_only_mode = True
        else:
            name_body = raw
            if slash_index != -1 and index < slash_index:
                kind = "positional_only"
            elif kw_only_mode:
                kind = "keyword_only"
            else:
                kind = "positional_or_keyword"

            equals_index = _find_top_level_char(name_body, "=")
            required = equals_index == -1

        equals_index = _find_top_level_char(name_body, "=")
        before_default = (
            name_body if equals_index == -1 else name_body[:equals_index].strip()
        )
        colon_index = _find_top_level_char(before_default, ":")
        name = (
            before_default
            if colon_index == -1
            else before_default[:colon_index].strip()
        )

        params.append(
            {
                "name": name,
                "kind": kind,
                "required": required,
            }
        )

    return params


def _is_breaking_signature_change(master_sig: str, pr_sig: str) -> bool:
    """Return True if PR signature is call-incompatible with master."""
    master_params = _parse_signature_shape(master_sig)
    pr_params = _parse_signature_shape(pr_sig)

    pr_index = 0
    for master_param in master_params:
        found_index = -1
        for idx in range(pr_index, len(pr_params)):
            if pr_params[idx]["name"] == master_param["name"]:
                found_index = idx
                break

        if found_index == -1:
            return True

        for inserted_param in pr_params[pr_index:found_index]:
            if inserted_param["required"]:
                return True

        pr_param = pr_params[found_index]
        if pr_param["kind"] != master_param["kind"]:
            return True

        if not master_param["required"] and pr_param["required"]:
            return True

        pr_index = found_index + 1

    return any(trailing_param["required"] for trailing_param in pr_params[pr_index:])


def compare_methods(
    master: dict[str, str],
    pr: dict[str, str],
    class_name: str,
) -> tuple[list[str], list[str]]:
    blocking = []
    informational = []
    master_methods = set(master.keys())
    pr_methods = set(pr.keys())

    removed = master_methods - pr_methods
    if removed:
        blocking.append(f"REMOVED {class_name} methods: {sorted(removed)}")

    added = pr_methods - master_methods
    if added:
        informational.append(f"ADDED {class_name} methods: {sorted(added)}")

    for name in sorted(master_methods & pr_methods):
        m_sig = _normalize_sig(master[name])
        p_sig = _normalize_sig(pr[name])
        if m_sig != p_sig:
            if _is_breaking_signature_change(master[name], pr[name]):
                blocking.append(f"CHANGED {class_name}.{name}:")
                blocking.append(f"  master: {master[name]}")
                blocking.append(f"  pr:     {pr[name]}")
            else:
                informational.append(
                    f"CHANGED (non-blocking, annotation-only) {class_name}.{name}:"
                )
                informational.append(f"  master: {master[name]}")
                informational.append(f"  pr:     {pr[name]}")

    return blocking, informational


NOISE_EXPORTS = {
    # stdlib modules imported in __init__.py - implementation details, not public API
    "*",
    "base64",
    "datetime",
    "os",
    "platform",
    "random",
    "socket",
    "stat",
    "sys",
    "threading",
    "time",
    "traceback",
    # third-party imports that leaked into namespace
    "serial",
    "tabulate",
    "google.protobuf.json_format",
    # internal utility helpers that leaked into namespace
    "fixme",
    # any alias that starts with underscore (private)
}


def compare_exports(
    master: list[str],
    pr: list[str],
) -> tuple[list[str], list[str]]:
    blocking = []
    informational = []
    removed = set(master) - set(pr)
    real_removed = {r for r in removed if r not in NOISE_EXPORTS}
    noise_removed = removed - real_removed
    if real_removed:
        blocking.append(f"REMOVED exports: {sorted(real_removed)}")
    if noise_removed:
        informational.append(
            f"REMOVED (noise/implementation detail): {sorted(noise_removed)}"
        )
    added = set(pr) - set(master)
    if added:
        informational.append(f"ADDED exports: {sorted(added)}")
    return blocking, informational


def main() -> int:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <master.json> <pr.json>", file=sys.stderr)
        return 1

    with open(sys.argv[1], encoding="utf-8") as f:
        master = json.load(f)
    with open(sys.argv[2], encoding="utf-8") as f:
        pr = json.load(f)

    all_informational = []
    all_blocking = []

    for cls_key, cls_name in [
        ("node_methods", "Node"),
        ("mesh_interface_methods", "MeshInterface"),
    ]:
        blocking, informational = compare_methods(
            master.get(cls_key, {}), pr.get(cls_key, {}), cls_name
        )
        if informational:
            all_informational.append(f"{cls_name} API changes (informational):")
            all_informational.extend(informational)
        if blocking:
            all_blocking.extend(blocking)

    export_blocking, export_informational = compare_exports(
        master.get("top_level_exports", []), pr.get("top_level_exports", [])
    )
    if export_informational:
        all_informational.append("Top-level export changes (informational):")
        all_informational.extend(export_informational)
    if export_blocking:
        all_blocking.extend(export_blocking)

    if all_informational:
        print("\n".join(all_informational))
        print()

    if all_blocking:
        print("\n".join(all_blocking))
        print("\nBREAKING API changes detected vs master!")
        return 1

    print("No breaking API changes detected vs master (informational changes above).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
