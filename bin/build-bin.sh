#!/bin/bash

set -e

echo Building ubuntu binary
poetry install
POETRYDIR="$(poetry env info --path 2>/dev/null || true)"
if [[ -z ${POETRYDIR} || ! -f "${POETRYDIR}/bin/activate" ]]; then
	echo "Unable to resolve Poetry virtualenv activate script at ${POETRYDIR}/bin/activate" >&2
	exit 1
fi
# shellcheck disable=SC1090,SC1091
source "${POETRYDIR}/bin/activate"
pyinstaller -F -n meshtastic --collect-all meshtastic meshtastic/__main__.py
