"""Behavioral contracts for release provenance and standalone artifacts."""

from __future__ import annotations

import os
import shlex
import shutil
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


def _write_pyproject(
    repository: Path,
    version: str,
    *,
    metadata_section: str = "tool.poetry",
) -> None:
    """Write minimal package metadata consumed by the release verifier."""
    if metadata_section not in {"project", "tool.poetry"}:
        raise ValueError(f"unsupported metadata section: {metadata_section}")
    (repository / "pyproject.toml").write_text(
        f'[{metadata_section}]\nname = "mtjk"\nversion = "{version}"\n',
        encoding="utf-8",
    )


def _initialize_release_repository(
    tmp_path: Path,
    version: str = "1.2.3",
    *,
    metadata_section: str = "tool.poetry",
) -> Path:
    """Create a small repository with a trusted develop lineage and release tag."""
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "switch", "-c", "develop")
    _write_pyproject(repository, version, metadata_section=metadata_section)
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
    repository: Path,
    tag: str,
    *extra_args: str,
    env: dict[str, str] | None = None,
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
        env=env,
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
def test_release_verifier_accepts_pep621_project_metadata(tmp_path: Path) -> None:
    """PEP 621 project metadata should be accepted without legacy Poetry fields."""
    repository = _initialize_release_repository(tmp_path, metadata_section="project")

    result = _run_verifier(repository, "v1.2.3")

    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_release_verifier_accepts_partial_pep621_migration(tmp_path: Path) -> None:
    """Project metadata may fall back field-by-field during a Poetry migration."""
    repository = _initialize_release_repository(tmp_path)
    _git(repository, "tag", "-d", "v1.2.3")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "mtjk"\n' '[tool.poetry]\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    _git(repository, "add", "pyproject.toml")
    _git(repository, "commit", "-m", "partial PEP 621 migration")
    migrated_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "v1.2.3", migrated_commit)
    _git(repository, "update-ref", "refs/remotes/origin/develop", migrated_commit)

    result = _run_verifier(repository, "v1.2.3")

    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_release_verifier_rejects_conflicting_metadata_sections(tmp_path: Path) -> None:
    """Conflicting PEP 621 and Poetry declarations must fail closed."""
    repository = _initialize_release_repository(tmp_path)
    release_commit = _git(repository, "rev-parse", "v1.2.3^{commit}")
    _git(repository, "tag", "-d", "v1.2.3")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "mtjk"\nversion = "1.2.3"\n'
        '[tool.poetry]\nname = "mtjk"\nversion = "9.9.9"\n',
        encoding="utf-8",
    )
    _git(repository, "add", "pyproject.toml")
    _git(repository, "commit", "-m", "conflicting package metadata")
    conflicting_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "v1.2.3", conflicting_commit)
    _git(repository, "update-ref", "refs/remotes/origin/develop", conflicting_commit)
    assert conflicting_commit != release_commit

    result = _run_verifier(repository, "v1.2.3")

    assert result.returncode == 1
    assert "Conflicting release metadata for 'version'" in result.stderr


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
def test_release_verifier_rejects_wrong_package_name(tmp_path: Path) -> None:
    """A release tag must still identify the mtjk distribution."""
    repository = _initialize_release_repository(tmp_path)
    _git(repository, "tag", "-d", "v1.2.3")
    (repository / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "not-mtjk"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    _git(repository, "add", "pyproject.toml")
    _git(repository, "commit", "-m", "wrong package name")
    wrong_name_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "v1.2.3", wrong_name_commit)
    _git(repository, "update-ref", "refs/remotes/origin/develop", wrong_name_commit)

    result = _run_verifier(repository, "v1.2.3")

    assert result.returncode == 1
    assert "Expected package name 'mtjk', got 'not-mtjk'" in result.stderr


@pytest.mark.unit
def test_release_verifier_rejects_unsupported_tag_format(tmp_path: Path) -> None:
    """The release contract must reject tags outside the documented version grammar."""
    repository = _initialize_release_repository(tmp_path)

    result = _run_verifier(repository, "release-1.2.3")

    assert result.returncode == 1
    assert "is not a supported version tag" in result.stderr


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
def test_release_verifier_reports_git_ancestry_errors(tmp_path: Path) -> None:
    """Git failures must not be mislabeled as an ordinary ancestry rejection."""
    repository = _initialize_release_repository(tmp_path)
    real_git = shutil.which("git")
    assert real_git is not None
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "if 'merge-base' in sys.argv and '--is-ancestor' in sys.argv:\n"
        "    print('fatal: synthetic ancestry failure', file=sys.stderr)\n"
        "    raise SystemExit(128)\n"
        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    result = _run_verifier(repository, "v1.2.3", env=env)

    assert result.returncode == 1
    assert "Unable to check ancestry" in result.stderr
    assert "synthetic ancestry failure" in result.stderr


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


def _write_fake_standalone(
    path: Path,
    *,
    version: str,
    help_values: tuple[str, ...] = ("--version", "--support", "--list-fields"),
    field_values: tuple[str, ...] = (
        "Local config fields:",
        "Module config fields:",
    ),
) -> None:
    """Write a small executable implementing a configurable smoke-test surface."""
    help_text = " ".join(help_values)
    field_args = " ".join(shlex.quote(value) for value in field_values)
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'case "${1:-}" in\n'
        f"  --version) printf '%s\\n' {shlex.quote(version)} ;;\n"
        f"  --help) printf '%s\\n' {shlex.quote(help_text)} ;;\n"
        f"  --list-fields) printf '%s\\n' {field_args} ;;\n"
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
@pytest.mark.parametrize(
    ("surface", "missing", "operation"),
    [
        ("help", "--version", "--help"),
        ("help", "--support", "--help"),
        ("help", "--list-fields", "--help"),
        ("fields", "Local config fields:", "--list-fields"),
        ("fields", "Module config fields:", "--list-fields"),
    ],
)
def test_standalone_smoke_contract_rejects_missing_required_surface(
    tmp_path: Path, surface: str, missing: str, operation: str
) -> None:
    """Every required standalone help/schema value must be enforced independently."""
    help_values = ["--version", "--support", "--list-fields"]
    field_values = ["Local config fields:", "Module config fields:"]
    values = help_values if surface == "help" else field_values
    values.remove(missing)
    binary = tmp_path / "meshtastic"
    _write_fake_standalone(
        binary,
        version="1.2.3",
        help_values=tuple(help_values),
        field_values=tuple(field_values),
    )

    result = subprocess.run(
        [str(_STANDALONE_SMOKE), str(binary), "1.2.3"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert (
        f"standalone {operation} output is missing required text: {missing}"
        in result.stderr
    )


@pytest.mark.unit
def test_release_workflows_share_provenance_and_artifact_contracts() -> None:
    """Publishing workflows must consume the verifier outputs they trust."""
    pypi_path = _REPO_ROOT / ".github" / "workflows" / "pypi-publish.yml"
    assets_path = _REPO_ROOT / ".github" / "workflows" / "release-assets.yml"
    container_path = _REPO_ROOT / ".github" / "workflows" / "container-build.yaml"
    workflows = [pypi_path, assets_path, container_path]

    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        assert "ref: develop" in text
        assert "fetch-depth: 0" in text
        assert "persist-credentials: false" in text
        assert "refs/remotes/origin/develop" in text
        verifier_index = text.index("bin/verify_release_source.py")
        candidate_checkout_index = text.index("Checkout validated release commit")
        assert verifier_index < candidate_checkout_index
        assert "RELEASE_COMMIT: ${{ steps.release_source.outputs.commit }}" in text
        assert 'git checkout --detach "${RELEASE_COMMIT}"' in text

    pypi = pypi_path.read_text(encoding="utf-8")
    assert "tag: ${{ steps.release_source.outputs.tag }}" in pypi
    assert "version: ${{ steps.release_source.outputs.version }}" in pypi
    assert (
        "name: pypi-dist-${{ steps.release_source.outputs.tag }}-"
        "${{ steps.release_source.outputs.version }}"
    ) in pypi
    assert (
        "name: pypi-dist-${{ needs.build.outputs.tag }}-"
        "${{ needs.build.outputs.version }}"
    ) in pypi

    release_assets = assets_path.read_text(encoding="utf-8")
    assert (
        "RELEASE_VERSION: ${{ steps.release_source.outputs.version }}" in release_assets
    )
    assert (
        'bin/smoke-standalone.sh dist/meshtastic "${RELEASE_VERSION}"' in release_assets
    )
    assert (
        "name: standalone-release-assets-${{ steps.release_source.outputs.tag }}"
        in release_assets
    )
    assert "tag_name: ${{ steps.release_source.outputs.tag }}" in release_assets

    container = container_path.read_text(encoding="utf-8")
    assert "RELEASE_VERSION: ${{ steps.release_source.outputs.version }}" in container
    assert 'echo "git_sha=${RELEASE_COMMIT}"' in container
    assert 'echo "docker_tag=${RELEASE_VERSION}"' in container
    assert (
        "org.opencontainers.image.version=${{ steps.build_info.outputs.docker_tag }}"
        in container
    )
    assert (
        "org.opencontainers.image.revision=${{ steps.build_info.outputs.git_sha }}"
        in container
    )
    assert "VCS_REF=${{ steps.build_info.outputs.git_sha }}" in container
    assert "VERSION=${{ steps.build_info.outputs.docker_tag }}" in container
    assert "flavor: |\n            latest=false" in container
    assert (
        "type=raw,value=latest,enable=${{ !github.event.release.prerelease }}"
        in container
    )
    assert "provenance: mode=max" in container
    assert "sbom: true" in container
