#!/usr/bin/env bash
set -euo pipefail

# Generate a baseline from a git ref (default: origin/master) using the current
# extractor script, without checking out that ref in the working tree.

MASTER_REF="${1:-origin/master}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
OUT_FILE="${REPO_ROOT}/meshtastic/tests/api_baselines/api_baseline_master.json"

if ! git -C "${REPO_ROOT}" rev-parse --verify "${MASTER_REF}" >/dev/null 2>&1; then
    echo "error: git ref '${MASTER_REF}' not found" >&2
    exit 1
fi

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/meshtastic-master-baseline.XXXXXX")"
cleanup() {
    rm -rf "${tmpdir}"
}
trap cleanup EXIT

git -C "${REPO_ROOT}" archive "${MASTER_REF}" meshtastic | tar -x -C "${tmpdir}"

(
    cd "${REPO_ROOT}"
    poetry run python bin/extract_api_surface.py "${tmpdir}/meshtastic" > "${OUT_FILE}"
)

echo "Generated ${OUT_FILE} from ${MASTER_REF}"
