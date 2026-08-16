#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Copyright (C) 2025 Olliver Schinagl <oliver@schinagl.nl>
#
# A beginning user should be able to docker run image bash (or sh) without
# needing to learn about --entrypoint
# https://github.com/docker-library/official-images#consistency

set -eu

# Derive the preferred CLI from installed distribution metadata without
# importing the meshtastic package (and therefore without loading optional deps).
bin="$(
	python - <<'PY'
from importlib.metadata import packages_distributions

distributions = packages_distributions().get("meshtastic", ())
print(distributions[0] if distributions else "meshtastic")
PY
)"
if ! command -v "${bin}" >/dev/null 2>&1; then
	# Compatibility fallback for unusual/upstream-oriented installations.
	bin='meshtastic'
fi

# run command if it is not starting with a "-" and is an executable in PATH
if [ "${#}" -le 0 ] ||
	[ "${1#-}" != "${1}" ] ||
	[ -d "${1}" ] ||
	! command -v "${1}" >'/dev/null' 2>&1; then
	entrypoint='true'
fi

exec ${entrypoint:+${bin:?}} "${@}"

exit 0
