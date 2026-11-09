#!/usr/bin/env bash
# =============================================================================
# Lake of Tears — One-Command Local Kubernetes Setup (setup-local.sh)
# =============================================================================
# Automates local workstation deployment on Kubernetes (OrbStack, Docker Desktop,
# minikube, kind, k3s):
#   1. Verifies docker, kubectl, and helm prerequisites
#   2. Detects cluster connectivity (auto-starts OrbStack k8s via orbctl if stopped)
#   3. Ensures ingress-nginx controller is installed
#   4. Builds local container images (backend, ui, airflow)
#   5. Deploys the Lake of Tears Helm chart with values-local.yaml
#   6. Verifies platform readiness and prints local access URLs
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

# Default options
NAMESPACE="lake-of-tears"
VALUES_FILE="${REPO_ROOT}/deploy/helm/values-local.yaml"
SKIP_BUILD="false"
SKIP_INGRESS="false"
SKIP_HEALTH="false"
TIMEOUT="10m"
DRY_RUN=""

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

One-command local Kubernetes setup for Lake of Tears.

Options:
  -n, --namespace <name>       Target Kubernetes namespace (default: lake-of-tears)
  -f, --values <path>          Path to Helm values file (default: deploy/helm/values-local.yaml)
      --skip-build             Skip building local Docker images
      --skip-ingress           Skip checking/installing ingress-nginx controller
      --skip-health-check      Skip post-deployment /api/health verification
      --dry-run                Simulate Helm installation without building images or making cluster changes
      --timeout <duration>     Helm install wait timeout (default: 10m)
  -h, --help                   Show this help message and exit

Examples:
  bash deploy/scripts/setup-local.sh
  ./lot setup-local
  ./lot setup-local --skip-build
EOF
    exit 0
}

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        -f|--values)
            VALUES_FILE="$2"
            shift 2
            ;;
        --skip-build)
            SKIP_BUILD="true"
            shift
            ;;
        --skip-ingress)
            SKIP_INGRESS="true"
            shift
            ;;
        --skip-health-check)
            SKIP_HEALTH="true"
            shift
            ;;
        --dry-run)
            DRY_RUN="--dry-run"
            SKIP_BUILD="true"
            SKIP_INGRESS="true"
            SKIP_HEALTH="true"
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

log_header "Lake of Tears — Local Kubernetes Quickstart"

# 1. Prerequisite Checks (docker, kubectl, helm)
log_info "Checking local CLI prerequisites (docker, kubectl, helm)..."
for cmd in docker kubectl helm; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        log_error "${cmd} is not installed or not in PATH. Please install ${cmd} and try again."
        exit 1
    fi
done
log_success "All CLI tools present (docker, kubectl, helm)."

# 2. Cluster Detection & OrbStack Auto-Start
log_info "Checking Kubernetes cluster connectivity..."
ensure_cluster_connectivity || exit 1
log_success "Connected to Kubernetes cluster ($(kubectl config current-context 2>/dev/null || echo 'default'))."

# 3. Ingress Controller Setup
if [[ "${SKIP_INGRESS}" != "true" ]]; then
    ensure_ingress_nginx || exit 1
else
    log_info "Skipping ingress-nginx controller check (--skip-ingress)."
fi

# 4. Build Local Docker Images
if [[ "${SKIP_BUILD}" != "true" ]]; then
    build_local_images || exit 1
else
    log_info "Skipping local Docker image builds (--skip-build)."
fi

# 5. Ensure Local Helm Values File Exists
if [[ ! -f "${VALUES_FILE}" ]]; then
    EXAMPLE_VALUES="${REPO_ROOT}/deploy/helm/values-local.example.yaml"
    if [[ -f "${EXAMPLE_VALUES}" ]]; then
        log_info "Creating ${VALUES_FILE} from ${EXAMPLE_VALUES}..."
        cp "${EXAMPLE_VALUES}" "${VALUES_FILE}"
        log_success "Created ${VALUES_FILE}"
    else
        log_error "Values file not found at ${VALUES_FILE} and example template is missing."
        exit 1
    fi
fi

# 6. Deploy Helm Chart
log_info "Deploying Lake of Tears Helm chart into namespace '${NAMESPACE}'..."
helm upgrade --install lake-of-tears "${REPO_ROOT}/deploy/helm/lake-of-tears" \
    --namespace "${NAMESPACE}" \
    --create-namespace \
    -f "${VALUES_FILE}" \
    ${DRY_RUN} \
    --wait --timeout "${TIMEOUT}"

log_success "Helm release 'lake-of-tears' deployed successfully."

# 7. Wait & Verification
HEALTH_STATUS="ONLINE"
if [[ "${SKIP_HEALTH}" != "true" ]]; then
    log_info "Verifying platform health at http://localhost/api/health..."
    if ! poll_health "http://localhost/api/health" 45; then
        HEALTH_STATUS="STARTING (Check pods with: kubectl get pods -n ${NAMESPACE})"
    fi
fi

# 8. Output Formatted Summary Box
echo -e "\n${BOLD}${GREEN}===============================================================================${RESET}"
echo -e "${BOLD}${GREEN}               LAKE OF TEARS — LOCAL KUBERNETES READY                          ${RESET}"
echo -e "${BOLD}${GREEN}===============================================================================${RESET}"
echo -e "  ${BOLD}Status:${RESET}          ${HEALTH_STATUS}"
echo -e "  ${BOLD}Namespace:${RESET}       ${NAMESPACE}"
echo -e "  ${BOLD}Values File:${RESET}     ${VALUES_FILE}"
echo -e "-------------------------------------------------------------------------------"
echo -e "  ${BOLD}Lake UI:${RESET}         ${CYAN}http://localhost${RESET}"
echo -e "  ${BOLD}Auth API:${RESET}        ${CYAN}http://localhost/api/${RESET}"
echo -e "  ${BOLD}Pipelines:${RESET}       ${CYAN}http://localhost/airflow/${RESET}"
echo -e "  ${BOLD}Superset BI:${RESET}     ${CYAN}http://superset.localhost${RESET}"
echo -e "  ${BOLD}MinIO Console:${RESET}   ${CYAN}http://minio.localhost${RESET}"
echo -e "  ${BOLD}MinIO S3 API:${RESET}    ${CYAN}http://s3.localhost${RESET}"
echo -e "-------------------------------------------------------------------------------"
echo -e "  ${BOLD}Day-0 First Setup:${RESET}"
echo -e "    Open ${BOLD}http://localhost/login${RESET} in your browser to create the root superadmin account."
echo -e "${BOLD}${GREEN}===============================================================================${RESET}\n"
