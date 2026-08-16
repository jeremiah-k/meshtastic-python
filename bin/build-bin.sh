#!/usr/bin/env bash

set -euo pipefail

echo "Building Ubuntu binary"
poetry install --extras cli --with dev

distribution_name="$(poetry run python -c 'from meshtastic._branding import DISTRIBUTION_NAME; print(DISTRIBUTION_NAME)')"
primary_cli="$(poetry run python -c 'from meshtastic._branding import PRIMARY_CLI_NAME; print(PRIMARY_CLI_NAME)')"
compatibility_cli_list="$(poetry run python -c 'from meshtastic._branding import COMPATIBILITY_CLI_NAMES; print(" ".join(COMPATIBILITY_CLI_NAMES))')"
read -r -a compatibility_clis <<<"${compatibility_cli_list}"

poetry run pyinstaller \
	--clean \
	--noconfirm \
	-F \
	-n "${primary_cli}" \
	--copy-metadata "${distribution_name}" \
	--collect-all meshtastic \
	meshtastic/__main__.py

for compatibility_cli in "${compatibility_clis[@]}"; do
	[[ -n ${compatibility_cli} ]] || continue
	cp "dist/${primary_cli}" "dist/${compatibility_cli}"
done
