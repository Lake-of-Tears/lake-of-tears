#!/usr/bin/env bash
# =============================================================================
# Lake of Tears — Platform Status & Health Inspection
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

NAMESPACE="lake-of-tears"
CONFIG_PATH=""

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Inspect the deployment health, pod status, and service URLs of Lake of Tears.

Options:
  -n, --namespace <name>   Kubernetes namespace to inspect (default: lake-of-tears)
  -c, --config <path>      Path to .toon configuration file
  -h, --help               Show this help message and exit

Examples:
  $(basename "$0")
  $(basename "$0") -n custom-namespace
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        -c|--config)
            CONFIG_PATH="$2"
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

log_header "Lake of Tears — Platform Status"

# Check kubectl
if ! command -v kubectl >/dev/null 2>&1; then
    log_error "kubectl is not installed or not in PATH."
    exit 1
fi

# Helm Release Status
echo -e "${BOLD}Helm Release:${RESET}"
if command -v helm >/dev/null 2>&1 && helm status lake-of-tears -n "${NAMESPACE}" >/dev/null 2>&1; then
    helm list -n "${NAMESPACE}" --filter "^lake-of-tears$"
else
    echo -e "  ${YELLOW}No active 'lake-of-tears' release found in namespace '${NAMESPACE}'.${RESET}"
fi

echo ""
echo -e "${BOLD}Pod Workloads in namespace '${NAMESPACE}':${RESET}"
kubectl get pods -n "${NAMESPACE}" -o wide 2>/dev/null || echo -e "  ${DIM}No pods running.${RESET}"

echo ""
# Resolve URLs if config can be found
ensure_python_runtime || exit 0
CONFIG_FILE="$(resolve_config "${CONFIG_PATH}" 2>/dev/null || true)"
if [[ -n "${CONFIG_FILE}" && -f "${CONFIG_FILE}" ]]; then
    echo -e "${BOLD}Configured Endpoints (${CONFIG_FILE}):${RESET}"
    ${LOT_PYTHON} "${REPO_ROOT}/scripts/lifecycle_helper.py" extract-urls -i "${CONFIG_FILE}" 2>/dev/null || true
fi
