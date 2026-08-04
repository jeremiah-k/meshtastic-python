#!/usr/bin/env python3
"""Verify standalone quality-tool pins agree with repository configuration."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_FILE = REPO_ROOT / "tools" / "quality_versions.env"
TRUNK_CONFIG = REPO_ROOT / ".trunk" / "trunk.yaml"
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _load_env_values() -> dict[str, str]:
    """Parse the canonical versions file without executing it as shell code."""
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        VERSIONS_FILE.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not _ENV_NAME_PATTERN.fullmatch(name) or not value:
            raise RuntimeError(
                f"Invalid NAME=value entry in {VERSIONS_FILE}:{line_number}: {raw_line!r}"
            )
        if name in values:
            raise RuntimeError(
                f"Duplicate {name} entry in {VERSIONS_FILE}:{line_number}"
            )
        values[name] = value
    return values


def _load_env_version(name: str) -> str:
    """Return one required version from the canonical versions file."""
    values = _load_env_values()
    try:
        return values[name]
    except KeyError as exc:
        raise RuntimeError(f"Missing {name} in {VERSIONS_FILE}") from exc


def _validated_ruff_version() -> str:
    """Return the canonical Ruff version after validating the Trunk pin."""
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
    return ruff_version


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse command-line options for CI/local version consumers."""
    parser = argparse.ArgumentParser(description=__doc__)
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--github-env",
        type=Path,
        metavar="PATH",
        help="append validated quality-tool variables to a GitHub Actions env file",
    )
    output.add_argument(
        "--print-ruff-version",
        action="store_true",
        help="print only the validated Ruff version",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate configured standalone tool versions and return a process status."""
    args = _parse_args(argv)
    ruff_version = _validated_ruff_version()
    if args.github_env is not None:
        with args.github_env.open("a", encoding="utf-8") as env_file:
            env_file.write(f"RUFF_VERSION={ruff_version}\n")
    if args.print_ruff_version:
        print(ruff_version)
    else:
        print(f"Quality tool versions are coherent (ruff {ruff_version}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
