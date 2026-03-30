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
    type_str = re.sub(r"\bUnion\[([^,]+),\s*None\]", r"Optional[\1]", type_str)
    type_str = re.sub(r"\bOptional\[([^]]+)\]", r"\1 | None", type_str)
    type_str = re.sub(r"typing\.", "", type_str)
    return type_str


def _normalize_sig(sig: str) -> str:
    def replacer(m):
        name = m.group(1)
        type_start = m.group(0).find(":") + 1
        type_part = m.group(0)[type_start:].strip()
        normalized = _normalize_type(type_part)
        return f"{name}: {normalized}"

    return re.sub(r"(\w+):\s*[^,)=]+", replacer, sig)


def compare_methods(
    master: dict, pr: dict, class_name: str
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
            informational.append(f"CHANGED {class_name}.{name}:")
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


def compare_exports(master: list, pr: list) -> tuple[list[str], list[str]]:
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


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <master.json> <pr.json>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        master = json.load(f)
    with open(sys.argv[2]) as f:
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
        sys.exit(1)
    else:
        print(
            "No breaking API changes detected vs master (informational changes above)."
        )


if __name__ == "__main__":
    main()
