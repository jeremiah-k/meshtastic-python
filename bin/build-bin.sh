#!/usr/bin/env bash

set -euo pipefail

echo "Building Ubuntu binary"
poetry install --extras cli --with dev
poetry run pyinstaller \
	--clean \
	--noconfirm \
	-F \
	-n meshtastic \
	--collect-all meshtastic \
	meshtastic/__main__.py
