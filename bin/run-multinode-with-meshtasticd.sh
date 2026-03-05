#!/usr/bin/env bash

set -euo pipefail

MESHTASTICD_IMAGE="${MESHTASTICD_IMAGE:-meshtastic/meshtasticd:latest}"
MESHTASTICD_CONTAINER_A="${MESHTASTICD_CONTAINER_A:-meshtasticd-multinode-a}"
MESHTASTICD_CONTAINER_B="${MESHTASTICD_CONTAINER_B:-meshtasticd-multinode-b}"
MESHTASTICD_HOST_A="${MESHTASTICD_HOST_A:-localhost:4403}"
MESHTASTICD_HOST_B="${MESHTASTICD_HOST_B:-localhost:4404}"
MESHTASTICD_PORT_A="${MESHTASTICD_PORT_A:-4403}"
MESHTASTICD_PORT_B="${MESHTASTICD_PORT_B:-4404}"
MESHTASTICD_HWID_A="${MESHTASTICD_HWID_A:-11}"
MESHTASTICD_HWID_B="${MESHTASTICD_HWID_B:-22}"
MESHTASTICD_READY_TIMEOUT_SECONDS="${MESHTASTICD_READY_TIMEOUT_SECONDS:-180}"
READY_LOG_A="${READY_LOG_A-}"
READY_LOG_B="${READY_LOG_B-}"
SMOKEVIRT_PYTEST_ARGS="${SMOKEVIRT_PYTEST_ARGS-}"
MESHTASTICD_PYTEST_TARGETS="${MESHTASTICD_PYTEST_TARGETS:-meshtastic/tests/test_meshtasticd_multinode_ci.py}"
MESHTASTICD_PYTEST_MARK_EXPR="${MESHTASTICD_PYTEST_MARK_EXPR:-int}"
EXTRA_PYTEST_ARGS=()
PYTEST_TARGETS=()
READY_LOG_A_IS_TEMP=false
READY_LOG_B_IS_TEMP=false

require_regex() {
	local value=$1
	local pattern=$2
	local name=$3
	if [[ ! ${value} =~ ${pattern} ]]; then
		echo "Invalid ${name}: ${value}" >&2
		exit 1
	fi
}

cleanup() {
	local exit_code=$?
	for container in "${MESHTASTICD_CONTAINER_A}" "${MESHTASTICD_CONTAINER_B}"; do
		if docker ps -a --format '{{.Names}}' | grep -Fxq "${container}"; then
			echo "===== meshtasticd logs (${container}) ====="
			docker logs "${container}" || true
			docker rm -f "${container}" >/dev/null || true
		fi
	done
	if [[ ${READY_LOG_A_IS_TEMP} == true ]]; then
		rm -f "${READY_LOG_A}" || true
	fi
	if [[ ${READY_LOG_B_IS_TEMP} == true ]]; then
		rm -f "${READY_LOG_B}" || true
	fi
	exit "${exit_code}"
}

trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
	echo "docker is required to run multinode meshtasticd integration checks." >&2
	exit 1
fi

OS_NAME="$(uname -s)"
if [[ ${OS_NAME} != "Linux" ]]; then
	echo "multinode meshtasticd runner currently requires Linux host networking." >&2
	exit 1
fi

require_regex "${MESHTASTICD_CONTAINER_A}" '^[A-Za-z0-9][A-Za-z0-9_.-]*$' "MESHTASTICD_CONTAINER_A"
require_regex "${MESHTASTICD_CONTAINER_B}" '^[A-Za-z0-9][A-Za-z0-9_.-]*$' "MESHTASTICD_CONTAINER_B"
require_regex "${MESHTASTICD_IMAGE}" '^[A-Za-z0-9][A-Za-z0-9._/-]*(:[A-Za-z0-9._-]+)?$' "MESHTASTICD_IMAGE"
require_regex "${MESHTASTICD_HOST_A}" '^[A-Za-z0-9._:-]+$' "MESHTASTICD_HOST_A"
require_regex "${MESHTASTICD_HOST_B}" '^[A-Za-z0-9._:-]+$' "MESHTASTICD_HOST_B"
require_regex "${MESHTASTICD_PORT_A}" '^[0-9]+$' "MESHTASTICD_PORT_A"
require_regex "${MESHTASTICD_PORT_B}" '^[0-9]+$' "MESHTASTICD_PORT_B"
require_regex "${MESHTASTICD_HWID_A}" '^[0-9]+$' "MESHTASTICD_HWID_A"
require_regex "${MESHTASTICD_HWID_B}" '^[0-9]+$' "MESHTASTICD_HWID_B"
require_regex "${MESHTASTICD_READY_TIMEOUT_SECONDS}" '^[0-9]+$' "MESHTASTICD_READY_TIMEOUT_SECONDS"
MESHTASTICD_PORT_A_DEC=$((10#${MESHTASTICD_PORT_A}))
MESHTASTICD_PORT_B_DEC=$((10#${MESHTASTICD_PORT_B}))
MESHTASTICD_READY_TIMEOUT_SECONDS_DEC=$((10#${MESHTASTICD_READY_TIMEOUT_SECONDS}))
if ((MESHTASTICD_PORT_A_DEC < 1 || MESHTASTICD_PORT_A_DEC > 65535)); then
	echo "MESHTASTICD_PORT_A must be between 1 and 65535." >&2
	exit 1
fi
if ((MESHTASTICD_PORT_B_DEC < 1 || MESHTASTICD_PORT_B_DEC > 65535)); then
	echo "MESHTASTICD_PORT_B must be between 1 and 65535." >&2
	exit 1
fi
if [[ -z ${READY_LOG_A} ]]; then
	READY_LOG_A="$(mktemp /tmp/meshtasticd-multinode-a-ready.XXXXXX.log)"
	READY_LOG_A_IS_TEMP=true
fi
if [[ -z ${READY_LOG_B} ]]; then
	READY_LOG_B="$(mktemp /tmp/meshtasticd-multinode-b-ready.XXXXXX.log)"
	READY_LOG_B_IS_TEMP=true
fi
if [[ ${READY_LOG_A} == *$'\n'* ]]; then
	echo "Invalid READY_LOG_A path." >&2
	exit 1
fi
if [[ ${READY_LOG_B} == *$'\n'* ]]; then
	echo "Invalid READY_LOG_B path." >&2
	exit 1
fi
if ((MESHTASTICD_READY_TIMEOUT_SECONDS_DEC <= 0)); then
	echo "MESHTASTICD_READY_TIMEOUT_SECONDS must be greater than zero." >&2
	exit 1
fi

: >"${READY_LOG_A}"
: >"${READY_LOG_B}"
docker rm -f "${MESHTASTICD_CONTAINER_A}" "${MESHTASTICD_CONTAINER_B}" >/dev/null 2>&1 || true

if ! docker pull "${MESHTASTICD_IMAGE}"; then
	if [[ ${MESHTASTICD_IMAGE} == "meshtastic/meshtasticd:latest" || ${MESHTASTICD_IMAGE} == "meshtastic/meshtasticd" ]]; then
		echo "Failed to pull ${MESHTASTICD_IMAGE}, retrying with meshtastic/meshtasticd:beta"
		MESHTASTICD_IMAGE="meshtastic/meshtasticd:beta"
		docker pull "${MESHTASTICD_IMAGE}"
	else
		echo "Failed to pull ${MESHTASTICD_IMAGE}" >&2
		exit 1
	fi
fi

docker run -d \
	--name "${MESHTASTICD_CONTAINER_A}" \
	--network host \
	"${MESHTASTICD_IMAGE}" \
	meshtasticd -s --fsdir=/var/lib/meshtasticd-a -p "${MESHTASTICD_PORT_A_DEC}" -h "${MESHTASTICD_HWID_A}" >/dev/null
docker run -d \
	--name "${MESHTASTICD_CONTAINER_B}" \
	--network host \
	"${MESHTASTICD_IMAGE}" \
	meshtasticd -s --fsdir=/var/lib/meshtasticd-b -p "${MESHTASTICD_PORT_B_DEC}" -h "${MESHTASTICD_HWID_B}" >/dev/null

wait_for_ready() {
	local host=$1
	local container=$2
	local ready_log_file=$3
	local deadline=$((SECONDS + MESHTASTICD_READY_TIMEOUT_SECONDS_DEC))

	until poetry run meshtastic --timeout 5 --host "${host}" --info >"${ready_log_file}" 2>&1; do
		if ! docker ps --format '{{.Names}}' | grep -Fxq "${container}"; then
			echo "${container} exited before becoming ready." >&2
			docker logs "${container}" >&2 || true
			return 1
		fi
		if ((SECONDS >= deadline)); then
			echo "${container} did not become ready within ${MESHTASTICD_READY_TIMEOUT_SECONDS}s." >&2
			echo "===== readiness output (${host}) =====" >&2
			cat "${ready_log_file}" >&2 || true
			docker logs "${container}" >&2 || true
			return 1
		fi
		sleep 2
	done
}

wait_for_ready "${MESHTASTICD_HOST_A}" "${MESHTASTICD_CONTAINER_A}" "${READY_LOG_A}" &
pid_ready_a=$!
wait_for_ready "${MESHTASTICD_HOST_B}" "${MESHTASTICD_CONTAINER_B}" "${READY_LOG_B}" &
pid_ready_b=$!

ready_status=0
wait "${pid_ready_a}" || ready_status=$?
wait "${pid_ready_b}" || ready_status=$?
if ((ready_status != 0)); then
	exit "${ready_status}"
fi

wait_for_log_pattern() {
	local container=$1
	local pattern=$2
	local timeout_seconds=${3:-30}
	local deadline=$((SECONDS + timeout_seconds))

	while ((SECONDS < deadline)); do
		if docker logs "${container}" 2>&1 | grep -Fq "${pattern}"; then
			return 0
		fi
		if ! docker ps --format '{{.Names}}' | grep -Fxq "${container}"; then
			echo "${container} exited while waiting for log pattern '${pattern}'." >&2
			docker logs "${container}" >&2 || true
			return 1
		fi
		sleep 1
	done

	echo "${container} did not emit expected log pattern '${pattern}' within ${timeout_seconds}s." >&2
	docker logs "${container}" >&2 || true
	return 1
}

wait_for_log_pattern "${MESHTASTICD_CONTAINER_A}" "Start multicast thread" 30 &
pid_log_a=$!
wait_for_log_pattern "${MESHTASTICD_CONTAINER_B}" "Start multicast thread" 30 &
pid_log_b=$!

log_status=0
wait "${pid_log_a}" || log_status=$?
wait "${pid_log_b}" || log_status=$?
if ((log_status != 0)); then
	exit "${log_status}"
fi

if [[ -n ${SMOKEVIRT_PYTEST_ARGS} ]]; then
	# Intentionally whitespace-split; keep args as simple tokens.
	read -r -a EXTRA_PYTEST_ARGS <<<"${SMOKEVIRT_PYTEST_ARGS}"
fi

read -r -a PYTEST_TARGETS <<<"${MESHTASTICD_PYTEST_TARGETS}"
if [[ ${#PYTEST_TARGETS[@]} -eq 0 ]]; then
	echo "MESHTASTICD_PYTEST_TARGETS must not be empty." >&2
	exit 1
fi

PYTEST_CMD=(poetry run pytest -m "${MESHTASTICD_PYTEST_MARK_EXPR}")
PYTEST_CMD+=("${PYTEST_TARGETS[@]}")
if [[ ${#EXTRA_PYTEST_ARGS[@]} -gt 0 ]]; then
	PYTEST_CMD+=("${EXTRA_PYTEST_ARGS[@]}")
fi
MESHTASTICD_HOST_A="${MESHTASTICD_HOST_A}" MESHTASTICD_HOST_B="${MESHTASTICD_HOST_B}" "${PYTEST_CMD[@]}"
