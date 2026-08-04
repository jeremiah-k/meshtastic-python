"""Tests for the standalone quality-tool version contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load_checker_module() -> ModuleType:
    """Load the repository quality-version checker as an isolated script module."""
    project_root = Path(__file__).resolve().parents[2]
    script_path = project_root / "bin" / "check_quality_tool_versions.py"
    spec = importlib.util.spec_from_file_location("check_quality_tool_versions", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_quality_version_parser_accepts_comments_and_export_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical version parser should accept normal dotenv-style formatting."""
    module = _load_checker_module()
    versions_file = tmp_path / "quality_versions.env"
    versions_file.write_text(
        "# standalone tools\n\nexport RUFF_VERSION = 0.16.1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "VERSIONS_FILE", versions_file)

    assert module._load_env_version("RUFF_VERSION") == "0.16.1"  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.parametrize(
    "contents",
    [
        "not-an-assignment\n",
        "RUFF_VERSION=\n",
        "RUFF_VERSION=0.16.1\nRUFF_VERSION=0.16.2\n",
    ],
)
def test_quality_version_parser_rejects_malformed_entries(
    contents: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed or duplicate version declarations should fail loudly."""
    module = _load_checker_module()
    versions_file = tmp_path / "quality_versions.env"
    versions_file.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(module, "VERSIONS_FILE", versions_file)

    with pytest.raises(RuntimeError):
        module._load_env_values()  # type: ignore[attr-defined]


@pytest.mark.unit
def test_quality_version_checker_writes_sanitized_github_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub Actions should receive only validated NAME=value entries."""
    module = _load_checker_module()
    versions_file = tmp_path / "quality_versions.env"
    versions_file.write_text(
        "# comment must never be copied to GITHUB_ENV\nRUFF_VERSION=0.16.1\n",
        encoding="utf-8",
    )
    trunk_file = tmp_path / "trunk.yaml"
    trunk_file.write_text("lint:\n  enabled:\n    - ruff@0.16.1\n", encoding="utf-8")
    github_env = tmp_path / "github-env"
    monkeypatch.setattr(module, "VERSIONS_FILE", versions_file)
    monkeypatch.setattr(module, "TRUNK_CONFIG", trunk_file)

    assert module.main(["--github-env", str(github_env)]) == 0  # type: ignore[attr-defined]
    assert github_env.read_text(encoding="utf-8") == "RUFF_VERSION=0.16.1\n"
