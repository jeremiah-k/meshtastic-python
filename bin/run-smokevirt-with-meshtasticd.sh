#!/usr/bin/env bash

set -euo pipefail

MESHTASTICD_IMAGE="${MESHTASTICD_IMAGE:-meshtastic/meshtasticd:latest}"
MESHTASTICD_CONTAINER="${MESHTASTICD_CONTAINER:-meshtasticd-smokevirt}"
MESHTASTICD_READY_TIMEOUT_SECONDS="${MESHTASTICD_READY_TIMEOUT_SECONDS:-120}"
READY_LOG_FILE="${READY_LOG_FILE:-/tmp/meshtasticd-smokevirt-ready.log}"
SMOKEVIRT_PYTEST_ARGS="${SMOKEVIRT_PYTEST_ARGS-}"
MESHTASTICD_PYTEST_TARGETS="${MESHTASTICD_PYTEST_TARGETS:-meshtastic/tests/test_meshtasticd_ci.py}"
MESHTASTICD_PYTEST_MARK_EXPR="${MESHTASTICD_PYTEST_MARK_EXPR-}"
EXTRA_PYTEST_ARGS=()
PYTEST_TARGETS=()
LOGS_PRINTED=false

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
	if [[ ${LOGS_PRINTED} == false ]] && docker ps -a --format '{{.Names}}' | grep -Fxq "${MESHTASTICD_CONTAINER}"; then
		echo "===== meshtasticd logs (${MESHTASTICD_CONTAINER}) ====="
		docker logs "${MESHTASTICD_CONTAINER}" || true
	fi
	if docker ps -a --format '{{.Names}}' | grep -Fxq "${MESHTASTICD_CONTAINER}"; then
		docker rm -f "${MESHTASTICD_CONTAINER}" >/dev/null || true
	fi
	exit "${exit_code}"
}

trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
	echo "docker is required to run smokevirt against meshtasticd." >&2
	exit 1
fi

require_regex "${MESHTASTICD_CONTAINER}" '^[A-Za-z0-9][A-Za-z0-9_.-]*$' "MESHTASTICD_CONTAINER"
require_regex "${MESHTASTICD_IMAGE}" '^[A-Za-z0-9][A-Za-z0-9._/-]*(:[A-Za-z0-9._-]+)?$' "MESHTASTICD_IMAGE"
require_regex "${MESHTASTICD_READY_TIMEOUT_SECONDS}" '^[0-9]+$' "MESHTASTICD_READY_TIMEOUT_SECONDS"
if [[ -z ${READY_LOG_FILE} || ${READY_LOG_FILE} == *$'\n'* ]]; then
	echo "Invalid READY_LOG_FILE path." >&2
	exit 1
fi
if ((10#${MESHTASTICD_READY_TIMEOUT_SECONDS} <= 0)); then
	echo "MESHTASTICD_READY_TIMEOUT_SECONDS must be greater than zero." >&2
	exit 1
fi

rm -f "${READY_LOG_FILE}"
docker rm -f "${MESHTASTICD_CONTAINER}" >/dev/null 2>&1 || true

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
	--name "${MESHTASTICD_CONTAINER}" \
	-p 4403:4403 \
	"${MESHTASTICD_IMAGE}" \
	meshtasticd -s --fsdir=/var/lib/meshtasticd >/dev/null

deadline=$((SECONDS + 10#${MESHTASTICD_READY_TIMEOUT_SECONDS}))
until poetry run meshtastic --timeout 5 --host localhost --info >"${READY_LOG_FILE}" 2>&1; do
	if ! docker ps --format '{{.Names}}' | grep -Fxq "${MESHTASTICD_CONTAINER}"; then
		echo "${MESHTASTICD_CONTAINER} exited before becoming ready." >&2
		if [[ -f ${READY_LOG_FILE} ]]; then
			echo "===== meshtastic readiness output =====" >&2
			cat "${READY_LOG_FILE}" >&2
		fi
		LOGS_PRINTED=true
		docker logs "${MESHTASTICD_CONTAINER}" >&2 || true
		exit 1
	fi
	if ((SECONDS >= deadline)); then
		echo "meshtasticd did not become ready within ${MESHTASTICD_READY_TIMEOUT_SECONDS}s." >&2
		if [[ -f ${READY_LOG_FILE} ]]; then
			echo "===== meshtastic readiness output =====" >&2
			cat "${READY_LOG_FILE}" >&2
		fi
		LOGS_PRINTED=true
		docker logs "${MESHTASTICD_CONTAINER}" >&2 || true
		exit 1
	fi
	sleep 2
done

if [[ -n ${SMOKEVIRT_PYTEST_ARGS} ]]; then
	# Intentionally whitespace-split; keep args as simple tokens.
	read -r -a EXTRA_PYTEST_ARGS <<<"${SMOKEVIRT_PYTEST_ARGS}"
fi

read -r -a PYTEST_TARGETS <<<"${MESHTASTICD_PYTEST_TARGETS}"

if [[ ${#PYTEST_TARGETS[@]} -eq 0 ]]; then
	echo "MESHTASTICD_PYTEST_TARGETS must not be empty." >&2
	exit 1
fi

if [[ -z ${MESHTASTICD_PYTEST_MARK_EXPR} ]]; then
	if [[ ${MESHTASTICD_PYTEST_TARGETS} =~ test_smokevirt\.py ]] && [[ ${MESHTASTICD_PYTEST_TARGETS} =~ test_meshtasticd_ci\.py ]]; then
		echo "MESHTASTICD_PYTEST_TARGETS includes both smokevirt and meshtasticd-ci targets; set MESHTASTICD_PYTEST_MARK_EXPR explicitly." >&2
		exit 1
	elif [[ ${MESHTASTICD_PYTEST_TARGETS} =~ test_smokevirt\.py ]]; then
		MESHTASTICD_PYTEST_MARK_EXPR="smokevirt and not smoke1_destructive"
	elif [[ ${MESHTASTICD_PYTEST_TARGETS} =~ test_meshtasticd_ci\.py ]]; then
		MESHTASTICD_PYTEST_MARK_EXPR="int"
	fi
fi

PYTEST_CMD=(poetry run pytest)
if [[ -n ${MESHTASTICD_PYTEST_MARK_EXPR} ]]; then
	PYTEST_CMD+=(-m "${MESHTASTICD_PYTEST_MARK_EXPR}")
fi
PYTEST_CMD+=("${PYTEST_TARGETS[@]}")
if [[ ${#EXTRA_PYTEST_ARGS[@]} -gt 0 ]]; then
	PYTEST_CMD+=("${EXTRA_PYTEST_ARGS[@]}")
fi
"${PYTEST_CMD[@]}"
