#!/usr/bin/env bash

set -euo pipefail

echo "Building Ubuntu binary"
poetry install
poetry run pyinstaller -F -n meshtastic --collect-all meshtastic meshtastic/__main__.py
