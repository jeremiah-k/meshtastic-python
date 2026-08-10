#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 3 ]]; then
	echo "Usage: $0 <title> <runner-script> <log-dir> [--no-logs]" >&2
	echo "  Provide a non-empty <log-dir> when log processing is expected." >&2
	echo "  Use --no-logs to explicitly skip log processing." >&2
	exit 1
fi

TITLE="$1"
RUNNER_SCRIPT="$2"
LOG_DIR="$3"
shift 3

NO_LOGS=false
while (($# > 0)); do
	case "$1" in
	--no-logs)
		NO_LOGS=true
		;;
	*)
		echo "Unexpected argument: $1. Expected optional --no-logs." >&2
		exit 1
		;;
	esac
	shift
done

log_directory_message=""
have_logs=false
log_files=()

if [[ -z ${LOG_DIR} ]]; then
	if [[ ${NO_LOGS} == true ]]; then
		log_directory_message="- Log directory: skipped (--no-logs)"
	else
		echo "- Log directory is required; pass --no-logs to skip processing." >&2
		exit 1
	fi
elif [[ ${NO_LOGS} == true ]]; then
	log_directory_message="- Log directory: skipped (--no-logs)"
elif [[ -d ${LOG_DIR} ]]; then
	shopt -s nullglob
	log_files=("${LOG_DIR}"/*.log)
	shopt -u nullglob
	if ((${#log_files[@]} == 0)); then
		log_directory_message="- Log files: none"
	else
		have_logs=true
	fi
else
	if [[ -e ${LOG_DIR} ]]; then
		echo "- Log path is not a directory: \`${LOG_DIR}\`" >&2
	else
		echo "- Log directory not found: \`${LOG_DIR}\`" >&2
	fi
	exit 1
fi

echo "### ${TITLE}"
echo ""
echo "- Runner script: \`${RUNNER_SCRIPT}\`"

if [[ -n ${log_directory_message} ]]; then
	echo "${log_directory_message}"
fi

if [[ ${have_logs} == false ]]; then
	exit 0
fi

echo "- Log files:"
for log_file in "${log_files[@]}"; do
	awk_counts="$(
		awk '
			/PACKET FROM PHONE|handleReceived|Forwarding to phone|FromRadio=STATE_SEND_PACKETS/ { packet_count++ }
			END { print NR + 0, packet_count + 0 }
		' "${log_file}"
	)"
	read -r line_count packetish_count <<<"${awk_counts}"
	echo "  - \`$(basename "${log_file}")\`: ${line_count} lines, packet-ish lines=${packetish_count}"
done
