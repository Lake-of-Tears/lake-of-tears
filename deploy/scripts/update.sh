#!/usr/bin/env bash
# =============================================================================
# Lake of Tears — TrueNAS-Style Platform Update Suite
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

# Default options
TRAIN="stable"
CHECK_ONLY="false"
OUTPUT_JSON="false"
CONFIG_PATH=""
NAMESPACE="lake-of-tears"
AUTO_CONFIRM="false"
NO_FETCH="false"
TIMEOUT="10m"

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Check for and apply platform updates for Lake of Tears (TrueNAS-style).

Options:
  -t, --train <stable|edge>   Update train/track (default: stable)
      --check-only            Only check for updates without modifying anything
      --json                  Output status in machine-readable JSON format
  -c, --config <path>         Path to .toon configuration file
  -n, --namespace <name>      Target Kubernetes namespace (default: lake-of-tears)
  -y, --yes                   Bypass interactive confirmation prompts
      --no-fetch              Skip remote Git fetch (offline/cached checks)
      --timeout <duration>    Helm upgrade wait timeout (default: 10m)
  -h, --help                  Show this help message and exit

Examples:
  $(basename "$0") --check-only
  $(basename "$0") --check-only --json
  $(basename "$0") --train edge
  $(basename "$0") --yes
EOF
    exit 0
}

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--train)
            TRAIN="$2"
            shift 2
            ;;
        --check-only)
            CHECK_ONLY="true"
            shift
            ;;
        --json)
            OUTPUT_JSON="true"
            shift
            ;;
        -c|--config)
            CONFIG_PATH="$2"
            shift 2
            ;;
        -n|--namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        -y|--yes)
            AUTO_CONFIRM="true"
            shift
            ;;
        --no-fetch)
            NO_FETCH="true"
            shift
            ;;
        --timeout)
            TIMEOUT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            log_error "Unknown argument: $1"
            usage
            ;;
    esac
done

ensure_python_runtime || exit 1

# 1. Query update status via lifecycle helper
FETCH_FLAG=""
if [[ "${NO_FETCH}" == "true" ]]; then
    FETCH_FLAG="--no-fetch"
fi

STATUS_RAW="$(${LOT_PYTHON} "${REPO_ROOT}/scripts/lifecycle_helper.py" check-updates --train "${TRAIN}" --json ${FETCH_FLAG} 2>/dev/null)" || true

if [[ -z "${STATUS_RAW}" ]]; then
    if [[ "${OUTPUT_JSON}" == "true" ]]; then
        echo '{"status": "ERROR", "error": "Failed to determine update status"}'
        exit 1
    fi
    log_error "Failed to query update status."
    exit 1
fi

# If caller requested JSON output with --check-only, print raw JSON and exit
if [[ "${OUTPUT_JSON}" == "true" && "${CHECK_ONLY}" == "true" ]]; then
    echo "${STATUS_RAW}"
    exit 0
fi

CAN_UPDATE="$(${LOT_PYTHON} -c "import json, sys; d=json.loads(sys.stdin.read()); print(str(d.get('can_update', False)).lower())" <<< "${STATUS_RAW}")"
CURRENT_VER="$(${LOT_PYTHON} -c "import json, sys; d=json.loads(sys.stdin.read()); print(d.get('current_version', 'unknown'))" <<< "${STATUS_RAW}")"
LATEST_VER="$(${LOT_PYTHON} -c "import json, sys; d=json.loads(sys.stdin.read()); print(d.get('latest_version', 'unknown'))" <<< "${STATUS_RAW}")"

if [[ "${OUTPUT_JSON}" != "true" ]]; then
    log_header "Lake of Tears — Platform Update Manager"
    echo -e "  ${BOLD}Update Train:${RESET}       ${CYAN}${TRAIN}${RESET}"
    echo -e "  ${BOLD}Installed Version:${RESET}  ${CURRENT_VER}"
    echo -e "  ${BOLD}Available Version:${RESET}  ${LATEST_VER}"
fi

# Check-only without JSON
if [[ "${CHECK_ONLY}" == "true" ]]; then
    if [[ "${CAN_UPDATE}" == "true" ]]; then
        echo -e "\n${BOLD}${GREEN}An update is available!${RESET} Run '${DIM}./lot update --train ${TRAIN}${RESET}' to apply."
        exit 0
    else
        echo -e "\n${GREEN}System is already up-to-date.${RESET}"
        exit 0
    fi
fi

if [[ "${CAN_UPDATE}" != "true" ]]; then
    if [[ "${OUTPUT_JSON}" == "true" ]]; then
        echo "${STATUS_RAW}"
        exit 0
    fi
    log_success "System is already up-to-date on train '${TRAIN}' (${CURRENT_VER})."
    exit 0
fi

# Display changelog preview
if [[ "${OUTPUT_JSON}" != "true" ]]; then
    echo -e "\n${BOLD}Pending Changes:${RESET}"
    ${LOT_PYTHON} -c "
import json, sys
d = json.loads(sys.stdin.read())
for line in d.get('changelog', [])[:10]:
    print(f'  * {line}')
" <<< "${STATUS_RAW}"
    echo ""
fi

# Interactive confirmation unless --yes is passed
if [[ "${AUTO_CONFIRM}" != "true" ]]; then
    if [[ -t 0 ]]; then
        read -r -p "Apply rolling update to ${LATEST_VER} now? [y/N] " confirm
        case "${confirm}" in
            [yY][eE][sS]|[yY])
                ;;
            *)
                log_info "Update cancelled by user."
                exit 0
                ;;
        esac
    else
        log_error "Non-interactive environment detected. Pass --yes to confirm update."
        exit 1
    fi
fi

# Pre-flight cluster checks
check_prerequisites || exit 1

# Synchronize Git repository
log_info "Updating repository to ${LATEST_VER}..."
if [[ "${TRAIN}" == "edge" ]]; then
    git pull --ff-only origin main
else
    # Fetch tag and checkout
    git checkout "${LATEST_VER}"
fi

# Resolve TOON configuration
CONFIG_FILE="$(resolve_config "${CONFIG_PATH}")"
log_info "Using configuration: ${CONFIG_FILE}"

# Execute atomic rolling Helm upgrade with automatic rollback on failure
log_info "Executing atomic rolling Helm upgrade..."
helm upgrade --install lake-of-tears "${REPO_ROOT}/deploy/helm/lake-of-tears" \
    --namespace "${NAMESPACE}" \
    -f <("${LOT_PYTHON}" "${REPO_ROOT}/scripts/toon2helm.py" -i "${CONFIG_FILE}") \
    --atomic --timeout "${TIMEOUT}"

log_success "Update to ${LATEST_VER} completed successfully."

# Extract URLs and check health
URLS_JSON="$(${LOT_PYTHON} "${REPO_ROOT}/scripts/lifecycle_helper.py" extract-urls -i "${CONFIG_FILE}" --json 2>/dev/null)" || URLS_JSON="{}"
LAKE_UI_URL="$(${LOT_PYTHON} -c "import json, sys; d=json.loads(sys.stdin.read()); print(d.get('lake_ui', ''))" <<< "${URLS_JSON}")"

if [[ -n "${LAKE_UI_URL}" ]]; then
    poll_health "${LAKE_UI_URL}/api/health" 45 || true
fi

if [[ "${OUTPUT_JSON}" == "true" ]]; then
    echo "{\"status\": \"SUCCESS\", \"version\": \"${LATEST_VER}\", \"train\": \"${TRAIN}\"}"
fi
