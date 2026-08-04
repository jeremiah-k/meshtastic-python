#!/usr/bin/env python3
"""Verify standalone quality-tool pins agree with repository configuration."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_FILE = REPO_ROOT / "tools" / "quality_versions.env"
TRUNK_CONFIG = REPO_ROOT / ".trunk" / "trunk.yaml"


def _load_env_version(name: str) -> str:
    """Return one ``NAME=value`` entry from the canonical versions file."""
    prefix = f"{name}="
    for raw_line in VERSIONS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise RuntimeError(f"Missing {name} in {VERSIONS_FILE}")


def main() -> int:
    """Validate configured standalone tool versions and return a process status."""
    ruff_version = _load_env_version("RUFF_VERSION")
    trunk_text = TRUNK_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"^\s*- ruff@([^\s]+)\s*$", trunk_text, flags=re.MULTILINE)
    if match is None:
        raise RuntimeError(f"Missing Ruff pin in {TRUNK_CONFIG}")
    trunk_version = match.group(1)
    if trunk_version != ruff_version:
        raise RuntimeError(
            "Ruff version mismatch: "
            f"tools/quality_versions.env={ruff_version}, trunk={trunk_version}"
        )
    print(f"Quality tool versions are coherent (ruff {ruff_version}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
