#!/bin/bash

set -e

echo Building ubuntu binary
poetry install
VENV_PATH=$(poetry env info --path)
source "${VENV_PATH}/bin/activate"
pyinstaller -F -n meshtastic --collect-all meshtastic meshtastic/__main__.py
