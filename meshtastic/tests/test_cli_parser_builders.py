"""Focused tests for CLI argument-group builders."""

import argparse

import pytest

from meshtastic.cli.parser import (
    addChannelConfigArgs,
    addConfigArgs,
    addConnectionArgs,
    addImportExportArgs,
    addLocalActionArgs,
    addPositionConfigArgs,
    addRemoteActionArgs,
    addRemoteAdminArgs,
    addSelectionArgs,
)


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(add_help=False)


@pytest.mark.unit
@pytest.mark.parametrize(
    "builder",
    [
        addConnectionArgs,
        addSelectionArgs,
        addImportExportArgs,
        addConfigArgs,
        addChannelConfigArgs,
        addPositionConfigArgs,
        addLocalActionArgs,
        addRemoteActionArgs,
        addRemoteAdminArgs,
    ],
)
def test_argument_builder_returns_same_parser(builder) -> None:
    parser = _parser()
    assert builder(parser) is parser


@pytest.mark.unit
def test_config_builder_parses_dynamic_modem_preset() -> None:
    parser = addConfigArgs(_parser())
    args = parser.parse_args(["--set", "lora.modem_preset", "medium-turbo"])
    assert args.set == [["lora.modem_preset", "medium-turbo"]]


@pytest.mark.unit
def test_local_action_builder_keeps_lockdown_actions_mutually_exclusive() -> None:
    parser = addLocalActionArgs(_parser())
    with pytest.raises(SystemExit):
        parser.parse_args(["--lockdown-unlock", "--lockdown-disable"])


@pytest.mark.unit
def test_connection_builder_accepts_tcp_host() -> None:
    args = addConnectionArgs(_parser()).parse_args(["--host", "localhost:4403"])
    assert args.host == "localhost:4403"


@pytest.mark.unit
def test_selection_builder_accepts_local_destination() -> None:
    args = addSelectionArgs(_parser()).parse_args(["--dest", "^local"])
    assert args.dest == "^local"
