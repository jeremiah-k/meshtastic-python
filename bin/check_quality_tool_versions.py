#!/usr/bin/env python3
"""Read standalone quality-tool versions from the Trunk configuration."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRUNK_CONFIG = REPO_ROOT / ".trunk" / "trunk.yaml"
# This intentionally follows Trunk's generated ``lint.enabled`` list-item format.
# A format change must fail loudly so CI never installs an unintended Ruff version.
_RUFF_PIN_PATTERN = re.compile(
    r"^[ \t]*-[ \t]+ruff@(?P<version>\d+\.\d+\.\d+)[ \t]*$",
    flags=re.MULTILINE,
)
_TOP_LEVEL_LINT_PATTERN = re.compile(r"^lint:[ \t]*$", flags=re.MULTILINE)
_ENABLED_PATTERN = re.compile(r"^(?P<indent>[ ]{2})enabled:[ \t]*$", flags=re.MULTILINE)


def _lint_enabled_block(trunk_text: str) -> str:
    """Return the text governed by Trunk's top-level ``lint.enabled`` key."""
    lint_matches = list(_TOP_LEVEL_LINT_PATTERN.finditer(trunk_text))
    if len(lint_matches) != 1:
        raise RuntimeError(
            f"Expected exactly one top-level lint section in {TRUNK_CONFIG}; "
            f"found {len(lint_matches)}"
        )

    lint_start = lint_matches[0].end()
    next_top_level = re.search(r"^\S", trunk_text[lint_start:], flags=re.MULTILINE)
    lint_end = (
        lint_start + next_top_level.start()
        if next_top_level is not None
        else len(trunk_text)
    )
    lint_block = trunk_text[lint_start:lint_end]

    enabled_matches = list(_ENABLED_PATTERN.finditer(lint_block))
    if len(enabled_matches) != 1:
        raise RuntimeError(
            f"Expected exactly one lint.enabled section in {TRUNK_CONFIG}; "
            f"found {len(enabled_matches)}"
        )

    enabled_match = enabled_matches[0]
    enabled_indent = len(enabled_match.group("indent"))
    enabled_start = enabled_match.end()
    enabled_lines: list[str] = []
    for line in lint_block[enabled_start:].splitlines():
        if line.strip() and len(line) - len(line.lstrip(" ")) <= enabled_indent:
            break
        enabled_lines.append(line)
    return "\n".join(enabled_lines)


def _ruff_version_from_trunk() -> str:
    """Return the single canonical Ruff version pinned by Trunk."""
    try:
        trunk_text = TRUNK_CONFIG.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"Unable to read Trunk configuration at {TRUNK_CONFIG}: {exc}"
        ) from exc

    matches = list(_RUFF_PIN_PATTERN.finditer(_lint_enabled_block(trunk_text)))
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one Ruff pin in {TRUNK_CONFIG} using the form "
            "'- ruff@X.Y.Z'; "
            f"found {len(matches)}"
        )
    return matches[0].group("version")


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
    """Publish the Ruff version configured by Trunk.

    Parameters
    ----------
    argv : Sequence[str] | None
        Command-line arguments to parse, or ``None`` to read from ``sys.argv``.

    Returns
    -------
    int
        Zero after publishing the configured version successfully.
    """
    args = _parse_args(argv)
    ruff_version = _ruff_version_from_trunk()
    if args.github_env is not None:
        with args.github_env.open("a", encoding="utf-8") as env_file:
            env_file.write(f"RUFF_VERSION={ruff_version}\n")
    if args.print_ruff_version:
        print(ruff_version)
    else:
        print(f"Ruff version from Trunk: {ruff_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
