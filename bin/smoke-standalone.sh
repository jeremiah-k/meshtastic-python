#!/usr/bin/env bash

set -euo pipefail

binary="${1:-dist/meshtastic}"
expected_version="${2:-}"

if [[ -z ${expected_version} ]]; then
	echo "usage: $0 <standalone-binary> <expected-version>" >&2
	exit 2
fi
if [[ ! -x ${binary} ]]; then
	echo "standalone binary is missing or not executable: ${binary}" >&2
	exit 1
fi

version_output="$("${binary}" --version)"
if [[ ${version_output} != "${expected_version}" ]]; then
	echo "standalone version mismatch: expected '${expected_version}', got '${version_output}'" >&2
	exit 1
fi

require_output() {
	local output="$1"
	local expected="$2"
	local operation="$3"
	if ! grep -Fq -- "${expected}" <<<"${output}"; then
		echo "standalone ${operation} output is missing required text: ${expected}" >&2
		exit 1
	fi
}

help_output="$("${binary}" --help)"
require_output "${help_output}" "--version" "--help"
require_output "${help_output}" "--support" "--help"
require_output "${help_output}" "--list-fields" "--help"

fields_output="$("${binary}" --list-fields)"
require_output "${fields_output}" "Local config fields:" "--list-fields"
require_output "${fields_output}" "Module config fields:" "--list-fields"

printf 'Standalone smoke test passed: %s (%s)\n' "${binary}" "${expected_version}"
