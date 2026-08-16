"""Behavioral contracts for release provenance and standalone artifacts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELEASE_VERIFIER = _REPO_ROOT / "bin" / "verify_release_source.py"
_STANDALONE_SMOKE = _REPO_ROOT / "bin" / "smoke-standalone.sh"


def _git(repository: Path, *args: str) -> str:
    """Run Git in a temporary repository and return stdout."""
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_pyproject(repository: Path, version: str) -> None:
    """Write the minimal package metadata consumed by the release verifier."""
    (repository / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "mtjk"\nversion = ' f'"{version}"\n',
        encoding="utf-8",
    )


def _initialize_release_repository(tmp_path: Path, version: str = "1.2.3") -> Path:
    """Create a small repository with a trusted develop lineage and release tag."""
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "switch", "-c", "develop")
    _write_pyproject(repository, version)
    _git(repository, "add", "pyproject.toml")
    _git(repository, "commit", "-m", "release source")
    _git(repository, "tag", f"v{version}")
    (repository / "after-release.txt").write_text(
        "later develop work\n", encoding="utf-8"
    )
    _git(repository, "add", "after-release.txt")
    _git(repository, "commit", "-m", "later develop work")
    develop_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-ref", "refs/remotes/origin/develop", develop_commit)
    return repository


def _run_verifier(
    repository: Path, tag: str, *extra_args: str
) -> subprocess.CompletedProcess[str]:
    """Execute the release verifier against a temporary repository."""
    return subprocess.run(
        [
            sys.executable,
            str(_RELEASE_VERIFIER),
            "--repository",
            str(repository),
            "--tag",
            tag,
            *extra_args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
def test_release_verifier_accepts_tag_reachable_from_develop(
    tmp_path: Path,
) -> None:
    """A correctly-versioned release tag in develop history should pass."""
    repository = _initialize_release_repository(tmp_path)
    output_file = tmp_path / "github-output"

    result = _run_verifier(
        repository,
        "v1.2.3",
        "--github-output",
        str(output_file),
    )

    assert result.returncode == 0, result.stderr
    output = output_file.read_text(encoding="utf-8")
    assert "tag=v1.2.3\n" in output
    assert "version=1.2.3\n" in output
    assert f"commit={_git(repository, 'rev-parse', 'v1.2.3^{commit}')}\n" in output


@pytest.mark.unit
def test_release_verifier_reads_package_metadata_from_tagged_commit(
    tmp_path: Path,
) -> None:
    """Later develop metadata must not replace the candidate tag's package version."""
    repository = _initialize_release_repository(tmp_path)
    _write_pyproject(repository, "9.9.9")
    _git(repository, "add", "pyproject.toml")
    _git(repository, "commit", "-m", "future package version")
    current_develop = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-ref", "refs/remotes/origin/develop", current_develop)

    result = _run_verifier(repository, "v1.2.3")

    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_release_verifier_rejects_tag_outside_develop_history(
    tmp_path: Path,
) -> None:
    """A release commit outside the trusted develop lineage must be rejected."""
    repository = _initialize_release_repository(tmp_path)
    trusted_develop = _git(repository, "rev-parse", "refs/remotes/origin/develop")
    _git(repository, "switch", "--orphan", "rogue")
    for path in repository.iterdir():
        if path.name != ".git" and path.is_file():
            path.unlink()
    _write_pyproject(repository, "1.2.4")
    _git(repository, "add", "pyproject.toml")
    _git(repository, "commit", "-m", "rogue release")
    _git(repository, "tag", "v1.2.4")
    _git(repository, "update-ref", "refs/remotes/origin/develop", trusted_develop)
    _git(repository, "checkout", "--detach", trusted_develop)

    result = _run_verifier(repository, "v1.2.4")

    assert result.returncode == 1
    assert "is not reachable from refs/remotes/origin/develop" in result.stderr


@pytest.mark.unit
def test_release_verifier_rejects_version_mismatch(tmp_path: Path) -> None:
    """Tag and package versions must match before publication."""
    repository = _initialize_release_repository(tmp_path)
    release_commit = _git(repository, "rev-parse", "v1.2.3^{commit}")
    _git(repository, "tag", "v1.2.4", release_commit)

    result = _run_verifier(repository, "v1.2.4")

    assert result.returncode == 1
    assert "Version mismatch" in result.stderr


@pytest.mark.unit
def test_release_verifier_rejects_untrusted_worktree_head(tmp_path: Path) -> None:
    """The verifier itself must run from the trusted develop checkout."""
    repository = _initialize_release_repository(tmp_path)
    release_commit = _git(repository, "rev-parse", "v1.2.3^{commit}")
    _git(repository, "checkout", "--detach", release_commit)

    result = _run_verifier(repository, "v1.2.3")

    assert result.returncode == 1
    assert "does not match trusted refs/remotes/origin/develop" in result.stderr


@pytest.mark.unit
def test_release_verifier_accepts_annotated_release_tag(tmp_path: Path) -> None:
    """Annotated tags should resolve to their underlying release commit."""
    repository = _initialize_release_repository(tmp_path)
    release_commit = _git(repository, "rev-parse", "v1.2.3^{commit}")
    _git(repository, "tag", "-d", "v1.2.3")
    _git(repository, "tag", "-a", "v1.2.3", release_commit, "-m", "release 1.2.3")

    result = _run_verifier(repository, "v1.2.3")

    assert result.returncode == 0, result.stderr


def _write_fake_standalone(path: Path, *, version: str) -> None:
    """Write a small executable implementing the standalone smoke-test contract."""
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'case "${1:-}" in\n'
        f"  --version) printf '%s\\n' '{version}' ;;\n"
        "  --help) printf '%s\\n' '--version --support --list-fields' ;;\n"
        "  --list-fields) printf '%s\\n' 'Local config fields:' 'Module config fields:' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.mark.unit
def test_standalone_smoke_contract_accepts_expected_cli_surface(tmp_path: Path) -> None:
    """The smoke helper should validate version, help, and schema introspection."""
    binary = tmp_path / "standalone build" / "meshtastic"
    binary.parent.mkdir()
    _write_fake_standalone(binary, version="1.2.3")

    result = subprocess.run(
        [str(_STANDALONE_SMOKE), str(binary), "1.2.3"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )

    assert result.returncode == 0, result.stderr
    assert "Standalone smoke test passed" in result.stdout


@pytest.mark.unit
def test_standalone_smoke_contract_rejects_wrong_version(tmp_path: Path) -> None:
    """The built artifact must report exactly the package version being released."""
    binary = tmp_path / "meshtastic"
    _write_fake_standalone(binary, version="9.9.9")

    result = subprocess.run(
        [str(_STANDALONE_SMOKE), str(binary), "1.2.3"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "standalone version mismatch" in result.stderr


@pytest.mark.unit
def test_release_workflows_share_provenance_and_artifact_contracts() -> None:
    """Every publishing workflow should consume the centralized release contract."""
    workflows = [
        _REPO_ROOT / ".github" / "workflows" / "pypi-publish.yml",
        _REPO_ROOT / ".github" / "workflows" / "release-assets.yml",
        _REPO_ROOT / ".github" / "workflows" / "container-build.yaml",
    ]
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        assert "ref: develop" in text
        assert "fetch-depth: 0" in text
        assert "persist-credentials: false" in text
        assert "refs/remotes/origin/develop" in text
        verifier_index = text.index("bin/verify_release_source.py")
        candidate_checkout_index = text.index("Checkout validated release commit")
        assert verifier_index < candidate_checkout_index

    release_assets = workflows[1].read_text(encoding="utf-8")
    assert "bin/smoke-standalone.sh" in release_assets

    container = workflows[2].read_text(encoding="utf-8")
    assert "provenance: mode=max" in container
    assert "sbom: true" in container
