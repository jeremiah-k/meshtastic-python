#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 3 ]]; then
	echo "Usage: $0 <title> <runner-script> <log-dir> [single|multinode] [--no-logs]" >&2
	echo "  Provide a non-empty <log-dir> when log processing is expected." >&2
	echo "  Use --no-logs to explicitly skip log processing." >&2
	exit 1
fi

TITLE="$1"
RUNNER_SCRIPT="$2"
LOG_DIR="$3"
MODE="${4:-single}"
NO_LOGS=false
if [[ ${4-} == "--no-logs" ]]; then
	MODE="single"
	NO_LOGS=true
	if (( $# > 4 )); then
		echo "Too many arguments. No further arguments are allowed after --no-logs." >&2
		exit 1
	fi
elif [[ ${5-} == "--no-logs" ]]; then
	NO_LOGS=true
	if (( $# > 5 )); then
		echo "Too many arguments. No arguments are allowed after --no-logs." >&2
		exit 1
	fi
elif (( $# > 4 )); then
	echo "Unexpected argument: ${5}. Expected '--no-logs' or no additional arguments." >&2
	exit 1
fi

if [[ ${MODE} != "single" && ${MODE} != "multinode" ]]; then
	echo "Invalid mode: ${MODE}. Expected 'single' or 'multinode'." >&2
	exit 1
fi

echo "### ${TITLE}"
echo ""
echo "- Runner script: \`${RUNNER_SCRIPT}\`"

if [[ -z ${LOG_DIR} ]]; then
	if [[ ${NO_LOGS} == true ]]; then
		echo "- Log directory: skipped (--no-logs)"
	else
		echo "- Log directory is required; pass --no-logs to skip processing." >&2
		exit 1
	fi
elif [[ ${NO_LOGS} == true ]]; then
	echo "- Log directory: skipped (--no-logs)"
elif [[ -d ${LOG_DIR} ]]; then
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
		summary="  - \`$(basename "${log_file}")\`: ${line_count} lines, packet-ish lines=${packetish_count}"
		if [[ ${MODE} == "multinode" ]]; then
			summary+=", multicast=${multicast_count}"
		fi
		echo "${summary}"
	done
else
	echo "- Log directory not found: \`${LOG_DIR}\`" >&2
	exit 1
fi
