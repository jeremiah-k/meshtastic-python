#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 3 ]]; then
	echo "Usage: $0 <title> <runner-script> <log-dir> [single|multinode]" >&2
	exit 1
fi

TITLE="$1"
RUNNER_SCRIPT="$2"
LOG_DIR="$3"
MODE="${4:-single}"

if [[ ${MODE} != "single" && ${MODE} != "multinode" ]]; then
	echo "Invalid mode: ${MODE}. Expected 'single' or 'multinode'." >&2
	exit 1
fi

echo "### ${TITLE}"
echo ""
echo "- Runner script: \`${RUNNER_SCRIPT}\`"

if [[ -d "${LOG_DIR}" ]]; then
	shopt -s nullglob
	log_files=("${LOG_DIR}"/*.log)
	shopt -u nullglob
	if ((${#log_files[@]} == 0)); then
		echo "- Log files: none"
		exit 0
	fi

	echo "- Log files:"
	for log_file in "${log_files[@]}"; do
		awk_counts="$(
			awk '
				/PACKET FROM PHONE|handleReceived|Forwarding to phone|FromRadio=STATE_SEND_PACKETS/ { packet_count++ }
				/Start multicast thread/ { multicast_count++ }
				END { print NR + 0, packet_count + 0, multicast_count + 0 }
			' "${log_file}"
		)"
		read -r line_count packetish_count multicast_count <<<"${awk_counts}"
		if [[ ${MODE} == "multinode" ]]; then
			echo "  - \`$(basename "${log_file}")\`: ${line_count} lines, packet-ish lines=${packetish_count}, multicast=${multicast_count}"
		else
			echo "  - \`$(basename "${log_file}")\`: ${line_count} lines, packet-ish lines=${packetish_count}"
		fi
	done
else
	echo "- Log directory not found: \`${LOG_DIR}\`"
fi
