#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 3 ]]; then
	echo "Usage: $0 <title> <runner-script> <log-dir> [single|multinode]" >&2
	exit 1
fi

TITLE=$1
RUNNER_SCRIPT=$2
LOG_DIR=$3
MODE=${4:-single}

echo "### ${TITLE}"
echo ""
echo "- Runner script: \`${RUNNER_SCRIPT}\`"

if [[ -d ${LOG_DIR} ]]; then
	shopt -s nullglob
	log_files=("${LOG_DIR}"/*.log)
	if ((${#log_files[@]} == 0)); then
		echo "- Log files: none"
		exit 0
	fi

	echo "- Log files:"
	for log_file in "${log_files[@]}"; do
		line_count="$(wc -l <"${log_file}" || echo 0)"
		packetish_count="$(grep -E -c 'PACKET FROM PHONE|handleReceived|Forwarding to phone|FromRadio=STATE_SEND_PACKETS' "${log_file}" || true)"
		if [[ ${MODE} == "multinode" ]]; then
			multicast_count="$(grep -F -c 'Start multicast thread' "${log_file}" || true)"
			echo "  - \`$(basename "${log_file}")\`: ${line_count} lines, packet-ish lines=${packetish_count}, multicast=${multicast_count}"
		else
			echo "  - \`$(basename "${log_file}")\`: ${line_count} lines, packet-ish lines=${packetish_count}"
		fi
	done
else
	echo "- Log directory not found: \`${LOG_DIR}\`"
fi
