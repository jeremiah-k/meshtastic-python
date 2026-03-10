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
shift 3

MODE="single"
NO_LOGS=false
mode_set=false
while (($# > 0)); do
	case "$1" in
	single | multinode)
		if [[ ${mode_set} == true ]]; then
			echo "Mode provided multiple times. Use one of: single, multinode." >&2
			exit 1
		fi
		MODE="$1"
		mode_set=true
		;;
	--no-logs)
		NO_LOGS=true
		;;
	*)
		echo "Unexpected argument: $1. Expected optional mode (single|multinode) and/or --no-logs." >&2
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
