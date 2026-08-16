#!/usr/bin/env python3
"""Verify that a release tag is a valid mtjk release from the develop lineage."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_RELEASE_TAG_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z]+)*$")
_EXPECTED_PACKAGE_NAME = "mtjk"


class ReleaseContractError(RuntimeError):
    """Raised when a candidate release violates the publication contract."""


@dataclass(frozen=True)
class ReleaseMetadata:
    """Validated release metadata emitted for downstream workflow steps."""

    tag: str
    version: str
    commit: str


def _git(
    repository: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run Git in ``repository`` and capture text output."""
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _parse_package_metadata(pyproject_text: str) -> tuple[str, str]:
    """Return Poetry package name and version from TOML text."""
    try:
        pyproject = tomllib.loads(pyproject_text)
        poetry = pyproject["tool"]["poetry"]
        return str(poetry["name"]), str(poetry["version"])
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseContractError(
            f"Unable to read Poetry release metadata: {exc}"
        ) from exc


def _read_tagged_package_metadata(repository: Path, commit: str) -> tuple[str, str]:
    """Read package metadata from the validated candidate commit, not the worktree."""
    try:
        pyproject_text = _git(repository, "show", f"{commit}:pyproject.toml").stdout
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise ReleaseContractError(
            f"Unable to read pyproject.toml from release commit {commit}: {detail}"
        ) from exc
    return _parse_package_metadata(pyproject_text)


def verify_release_source(
    repository: Path,
    *,
    tag: str,
    develop_ref: str = "refs/remotes/origin/develop",
) -> ReleaseMetadata:
    """Validate tag metadata and ancestry while executing from trusted develop.

    The verifier is intentionally expected to run while ``HEAD`` is the trusted
    ``develop_ref`` commit. Candidate package metadata is read directly from the
    tagged Git object. The caller should check out the returned commit only after
    this function succeeds.

    Parameters
    ----------
    repository : Path
        Git worktree currently checked out at the trusted develop commit.
    tag : str
        Release tag supplied by the GitHub release event.
    develop_ref : str
        Remote-tracking ref that defines the trusted release lineage.

    Returns
    -------
    ReleaseMetadata
        Validated tag, normalized version, and release commit.

    Raises
    ------
    ReleaseContractError
        If any release provenance or package metadata invariant fails.
    """
    if not _RELEASE_TAG_RE.fullmatch(tag):
        raise ReleaseContractError(
            f"Release tag {tag!r} is not a supported version tag"
        )
    version = tag.removeprefix("v")

    try:
        tag_commit = _git(
            repository, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"
        ).stdout.strip()
        head_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()
        develop_commit = _git(
            repository, "rev-parse", "--verify", develop_ref
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise ReleaseContractError(
            f"Unable to resolve release provenance: {detail}"
        ) from exc

    if head_commit != develop_commit:
        raise ReleaseContractError(
            f"Verifier checkout HEAD {head_commit} does not match trusted "
            f"{develop_ref} commit {develop_commit}"
        )

    ancestry = _git(
        repository,
        "merge-base",
        "--is-ancestor",
        tag_commit,
        develop_ref,
        check=False,
    )
    if ancestry.returncode != 0:
        raise ReleaseContractError(
            f"Release commit {tag_commit} is not reachable from {develop_ref}"
        )

    package_name, package_version = _read_tagged_package_metadata(
        repository, tag_commit
    )
    if package_name != _EXPECTED_PACKAGE_NAME:
        raise ReleaseContractError(
            f"Expected package name {_EXPECTED_PACKAGE_NAME!r}, got {package_name!r}"
        )
    if package_version != version:
        raise ReleaseContractError(
            f"Version mismatch: tag {tag!r} -> {version!r}, pyproject -> {package_version!r}"
        )

    return ReleaseMetadata(tag=tag, version=version, commit=tag_commit)


def _write_github_output(path: Path, metadata: ReleaseMetadata) -> None:
    """Append validated release metadata to a GitHub Actions output file."""
    with path.open("a", encoding="utf-8") as output:
        output.write(f"tag={metadata.tag}\n")
        output.write(f"version={metadata.version}\n")
        output.write(f"commit={metadata.commit}\n")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Release tag to validate")
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path.cwd(),
        help="Repository worktree (default: current directory)",
    )
    parser.add_argument(
        "--develop-ref",
        default="refs/remotes/origin/develop",
        help="Trusted remote-tracking branch ref",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        help="Optional GitHub Actions output file to append metadata to",
    )
    return parser.parse_args()


def main() -> int:
    """Run release verification and emit a concise validated summary."""
    args = _parse_args()
    try:
        metadata = verify_release_source(
            args.repository.resolve(),
            tag=args.tag,
            develop_ref=args.develop_ref,
        )
    except (OSError, ReleaseContractError) as exc:
        print(f"::error::Release contract verification failed: {exc}", file=sys.stderr)
        return 1

    if args.github_output is not None:
        _write_github_output(args.github_output, metadata)
    print(
        f"Validated release {metadata.tag} at {metadata.commit} "
        f"from {args.develop_ref}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
