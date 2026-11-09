#!/usr/bin/env bash
# =============================================================================
# Lake of Tears — Helm Platform Installation Suite
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

# Default options
CONFIG_PATH=""
NAMESPACE="lake-of-tears"
AUTO_GEN_SECRETS="false"
BUILD_IMAGES="false"
ENSURE_INGRESS="false"
FORCE="false"
SKIP_HEALTH="false"
TIMEOUT="10m"
DRY_RUN=""

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Install or upgrade Lake of Tears on Kubernetes using Helm and TOON configuration.

Options:
  -c, --config <path>          Path to .toon configuration file
  -n, --namespace <name>       Target Kubernetes namespace (default: lake-of-tears)
      --build                  Build local Docker images (backend, ui, airflow) before deploying
      --ensure-ingress         Check for nginx IngressClass and auto-install ingress-nginx if missing
      --auto-generate-secrets  Automatically generate random credentials for changeme-* placeholders
      --dry-run                Simulate Helm installation without making cluster changes
      --skip-health-check      Skip post-deployment /api/health verification
      --timeout <duration>     Helm install wait timeout (default: 10m)
  -f, --force                  Bypass interactive prompts and warnings
  -h, --help                   Show this help message and exit

Examples:
  $(basename "$0")
  $(basename "$0") --build --ensure-ingress --auto-generate-secrets
  $(basename "$0") --dry-run
  $(basename "$0") --config /etc/lake-of-tears/lake-of-tears.toon
  $(basename "$0") --auto-generate-secrets --namespace lot-dev
EOF
    exit 0
}

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c|--config)
            CONFIG_PATH="$2"
            shift 2
            ;;
        -n|--namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        --build)
            BUILD_IMAGES="true"
            shift
            ;;
        --ensure-ingress)
            ENSURE_INGRESS="true"
            shift
            ;;
        --auto-generate-secrets)
            AUTO_GEN_SECRETS="true"
            shift
            ;;
        --dry-run)
            DRY_RUN="--dry-run"
            SKIP_HEALTH="true"
            shift
            ;;
        --skip-health-check)
            SKIP_HEALTH="true"
            shift
            ;;
        --timeout)
            TIMEOUT="$2"
            shift 2
            ;;
        -f|--force)
            FORCE="true"
            shift
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

log_header "Lake of Tears — Platform Installation"

# 1. Prerequisite checks
check_prerequisites || exit 1

# 1a. Optional ingress-nginx controller setup
if [[ "${ENSURE_INGRESS}" == "true" && -z "${DRY_RUN}" ]]; then
    ensure_ingress_nginx || exit 1
fi

# 1b. Optional local container image build
if [[ "${BUILD_IMAGES}" == "true" && -z "${DRY_RUN}" ]]; then
    build_local_images || exit 1
fi

# 2. Config discovery
log_info "Resolving TOON configuration..."
CONFIG_FILE="$(resolve_config "${CONFIG_PATH}")"
log_success "Using configuration: ${CONFIG_FILE}"

# 3. Secret validation & auto-generation
handle_secrets "${CONFIG_FILE}" "${AUTO_GEN_SECRETS}" "${FORCE}" || exit 1

# 4. Ensure destination namespace exists
log_info "Ensuring namespace '${NAMESPACE}' exists..."
kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}"

# 5. Execute Helm deployment via dynamic TOON compilation
log_info "Compiling TOON specification and executing Helm deployment..."
log_info "Release: lake-of-tears | Namespace: ${NAMESPACE} | Timeout: ${TIMEOUT}"

# Process substitution streams compiled YAML directly into Helm without writing secrets to disk
helm upgrade --install lake-of-tears "${REPO_ROOT}/deploy/helm/lake-of-tears" \
    --namespace "${NAMESPACE}" \
    -f <("${LOT_PYTHON}" "${REPO_ROOT}/scripts/toon2helm.py" -i "${CONFIG_FILE}") \
    ${DRY_RUN} \
    --wait --timeout "${TIMEOUT}"

log_success "Helm release 'lake-of-tears' deployed successfully."

# 6. Extract deployed service URLs
URLS_JSON="$(${LOT_PYTHON} "${REPO_ROOT}/scripts/lifecycle_helper.py" extract-urls -i "${CONFIG_FILE}" --json 2>/dev/null)" || URLS_JSON="{}"

LAKE_UI_URL="$(${LOT_PYTHON} -c "import json, sys; d=json.loads(sys.stdin.read()); print(d.get('lake_ui', ''))" <<< "${URLS_JSON}")"
PIPELINES_URL="$(${LOT_PYTHON} -c "import json, sys; d=json.loads(sys.stdin.read()); print(d.get('pipelines', ''))" <<< "${URLS_JSON}")"
MINIO_URL="$(${LOT_PYTHON} -c "import json, sys; d=json.loads(sys.stdin.read()); print(d.get('minio_console', ''))" <<< "${URLS_JSON}")"
SUPERSET_URL="$(${LOT_PYTHON} -c "import json, sys; d=json.loads(sys.stdin.read()); print(d.get('superset', ''))" <<< "${URLS_JSON}")"

# 7. Post-install health check
HEALTH_STATUS="ONLINE"
if [[ "${SKIP_HEALTH}" != "true" && -n "${LAKE_UI_URL}" ]]; then
    log_info "Verifying platform health at ${LAKE_UI_URL}/api/health..."
    if ! poll_health "${LAKE_UI_URL}/api/health" 45; then
        HEALTH_STATUS="STARTING (Check pods with: kubectl get pods -n ${NAMESPACE})"
    fi
fi

# 8. Output Formatted Summary Box
echo -e "\n${BOLD}${GREEN}===============================================================================${RESET}"
echo -e "${BOLD}${GREEN}               LAKE OF TEARS — DEPLOYMENT SUMMARY                              ${RESET}"
echo -e "${BOLD}${GREEN}===============================================================================${RESET}"
echo -e "  ${BOLD}Status:${RESET}          ${HEALTH_STATUS}"
echo -e "  ${BOLD}Namespace:${RESET}       ${NAMESPACE}"
echo -e "  ${BOLD}Config:${RESET}          ${CONFIG_FILE}"
echo -e "-------------------------------------------------------------------------------"
echo -e "  ${BOLD}Lake UI:${RESET}         ${CYAN}${LAKE_UI_URL}${RESET}"
echo -e "  ${BOLD}Pipelines:${RESET}       ${CYAN}${PIPELINES_URL}${RESET}"
echo -e "  ${BOLD}MinIO Console:${RESET}   ${CYAN}${MINIO_URL}${RESET}"
echo -e "  ${BOLD}Superset BI:${RESET}     ${CYAN}${SUPERSET_URL}${RESET}"
echo -e "-------------------------------------------------------------------------------"
echo -e "  ${BOLD}Day-0 First Setup:${RESET}"
echo -e "    Open ${BOLD}${LAKE_UI_URL}/login${RESET} in your browser to create the root superadmin account."
echo -e "  ${BOLD}Useful Lifecycle Commands:${RESET}"
echo -e "    * Check health & pods:  ${DIM}./lot status${RESET}"
echo -e "    * Check for updates:    ${DIM}./lot update --check-only${RESET}"
echo -e "    * Apply updates:        ${DIM}./lot update${RESET}"
echo -e "    * Teardown release:     ${DIM}./lot destroy${RESET}"
echo -e "${BOLD}${GREEN}===============================================================================${RESET}\n"
