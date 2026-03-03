"""Tests for package-version resolution and fork-aware update checks."""

from importlib.metadata import PackageNotFoundError

import pytest
import requests

import meshtastic.util as util_module
import meshtastic.version as version_module


def _make_fake_response(version: str) -> object:
    """Create a minimal fake response object for PyPI version checks."""

    class _FakeResponse:
        """Stub response payload for the PyPI version endpoint."""

        def json(self) -> dict[str, dict[str, str]]:
            """Return fake PyPI response JSON."""
            return {"info": {"version": version}}

    return _FakeResponse()


@pytest.mark.unit
def test_get_active_version_prefers_mtjk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The active version lookup should prefer the fork distribution name."""

    def _fake_version(distribution_name: str) -> str:
        if distribution_name == "mtjk":
            return "2.7.8"
        raise PackageNotFoundError

    monkeypatch.setattr(version_module, "version", _fake_version)
    assert version_module.get_active_version() == "2.7.8"
    assert version_module.getActiveVersion() == "2.7.8"


@pytest.mark.unit
def test_get_active_version_falls_back_to_meshtastic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The active version lookup should fall back to upstream distribution name."""

    def _fake_version(distribution_name: str) -> str:
        if distribution_name == "meshtastic":
            return "2.7.8"
        raise PackageNotFoundError

    monkeypatch.setattr(version_module, "version", _fake_version)
    assert version_module.get_active_version() == "2.7.8"


@pytest.mark.unit
def test_get_active_version_returns_unknown_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Version lookup should return unknown when no distribution metadata is present."""

    def _fake_version(distribution_name: str) -> str:
        _ = distribution_name
        raise PackageNotFoundError

    monkeypatch.setattr(version_module, "version", _fake_version)
    assert version_module.get_active_version() == "unknown"


@pytest.mark.unit
def test_check_if_newer_version_falls_back_to_second_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyPI checks should fall back to the next configured distribution name."""

    calls: list[str] = []

    def _fake_get(url: str, timeout: float) -> object:
        _ = timeout
        calls.append(url)
        if "/mtjk/" in url:
            raise requests.RequestException("package not published yet")
        return _make_fake_response("2.7.9")

    monkeypatch.setattr(
        util_module,
        "DISTRIBUTION_NAME_CANDIDATES",
        ("mtjk", "meshtastic"),
    )
    monkeypatch.setattr("meshtastic.util.requests.get", _fake_get)
    monkeypatch.setattr(util_module, "get_active_version", lambda: "2.7.8")

    assert util_module.check_if_newer_version() == "2.7.9"
    assert calls == [
        "https://pypi.org/pypi/mtjk/json",
        "https://pypi.org/pypi/meshtastic/json",
    ]


@pytest.mark.unit
def test_check_if_newer_version_returns_none_when_not_newer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyPI checks should return None when the fetched version is not newer."""

    def _fake_get(url: str, timeout: float) -> object:
        _ = (url, timeout)
        return _make_fake_response("2.7.8")

    monkeypatch.setattr(
        util_module,
        "DISTRIBUTION_NAME_CANDIDATES",
        ("mtjk",),
    )
    monkeypatch.setattr("meshtastic.util.requests.get", _fake_get)
    monkeypatch.setattr(util_module, "get_active_version", lambda: "2.7.8")

    assert util_module.check_if_newer_version() is None
