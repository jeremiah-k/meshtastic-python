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


def _normalize_type(type_str: str) -> str:
    result = type_str
    result = re.sub(r"\bUnion\[([^,]+),\s*None\]", r"Optional[\1]", result)
    result = re.sub(r"\bOptional\[([^]]+)\]", r"\1 | None", result)
    result = re.sub(r"typing\.", "", result)
    return result


def _normalize_sig(sig: str) -> str:
    return re.sub(r"(\w+):\s*[^,)=]+", lambda m: m.group(0), sig)


def compare_methods(master: dict, pr: dict, class_name: str) -> list[str]:
    diffs = []
    master_methods = set(master.keys())
    pr_methods = set(pr.keys())

    removed = master_methods - pr_methods
    if removed:
        diffs.append(f"REMOVED {class_name} methods: {sorted(removed)}")

    added = pr_methods - master_methods
    if added:
        diffs.append(f"ADDED {class_name} methods: {sorted(added)}")

    for name in sorted(master_methods & pr_methods):
        m_sig = _normalize_sig(master[name])
        p_sig = _normalize_sig(pr[name])
        if m_sig != p_sig:
            diffs.append(f"CHANGED {class_name}.{name}:")
            diffs.append(f"  master: {master[name]}")
            diffs.append(f"  pr:     {pr[name]}")

    return diffs


def compare_exports(master: list, pr: list) -> list[str]:
    diffs = []
    removed = set(master) - set(pr)
    if removed:
        diffs.append(f"REMOVED exports: {sorted(removed)}")
    added = set(pr) - set(master)
    if added:
        diffs.append(f"ADDED exports: {sorted(added)}")
    return diffs


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <master.json> <pr.json>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        master = json.load(f)
    with open(sys.argv[2]) as f:
        pr = json.load(f)

    all_diffs = []

    for cls_key, cls_name in [
        ("node_methods", "Node"),
        ("mesh_interface_methods", "MeshInterface"),
    ]:
        diffs = compare_methods(master.get(cls_key, {}), pr.get(cls_key, {}), cls_name)
        if diffs:
            all_diffs.extend([f"{cls_name} API differences:"] + diffs)

    export_diffs = compare_exports(
        master.get("top_level_exports", []), pr.get("top_level_exports", [])
    )
    if export_diffs:
        all_diffs.extend(["Top-level export differences:"] + export_diffs)

    if all_diffs:
        print("\n".join(all_diffs))
        print("\nAPI differences detected vs master!")
        print(
            "Review changes above. ADDED items are informational; "
            "REMOVED items are potential breaking changes."
        )
        sys.exit(1)
    else:
        print("No API differences detected vs master")


if __name__ == "__main__":
    main()
