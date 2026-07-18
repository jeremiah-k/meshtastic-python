"""Focused tests for parser orchestration outside the legacy entry module."""

import argparse
from unittest.mock import MagicMock

import pytest

from meshtastic.cli.parser import parse_cli_args


@pytest.mark.unit
def test_parse_cli_args_registers_version_and_returns_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.argv", ["meshtastic"])
    parser = argparse.ArgumentParser(add_help=False)
    args = parse_cli_args(parser, version="9.9.9", argcomplete_module=None)
    assert isinstance(args, argparse.Namespace)


@pytest.mark.unit
def test_parse_cli_args_calls_optional_argcomplete(monkeypatch) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    autocomplete = MagicMock()
    module = MagicMock(autocomplete=autocomplete)
    monkeypatch.setattr("sys.argv", ["meshtastic"])
    parse_cli_args(parser, version="9.9.9", argcomplete_module=module)
    autocomplete.assert_called_once_with(parser)


@pytest.mark.unit
def test_parse_cli_args_version_action_uses_supplied_version(monkeypatch, capsys) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    monkeypatch.setattr("sys.argv", ["meshtastic", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        parse_cli_args(parser, version="9.9.9", argcomplete_module=None)
    assert excinfo.value.code == 0
    assert capsys.readouterr().out == "9.9.9\n"
