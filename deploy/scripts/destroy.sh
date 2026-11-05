#!/usr/bin/env bash
# =============================================================================
# Lake of Tears — Teardown & Lifecycle Destruction Suite
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

# Default options
NAMESPACE="lake-of-tears"
DATA_ACTION="" # "keep" or "purge"
FORCE="false"

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Uninstall Lake of Tears and manage persistent storage retention.

Options:
  -n, --namespace <name>   Kubernetes namespace to tear down (default: lake-of-tears)
      --keep-data          Preserve PersistentVolumeClaims (PVCs) for future re-attachment (Safe default)
      --purge-data         Permanently delete all PVCs and underlying data
  -f, --force              Bypass interactive confirmation safeguards
  -h, --help               Show this help message and exit

Examples:
  $(basename "$0")
  $(basename "$0") --keep-data
  $(basename "$0") --purge-data --force
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
        --keep-data)
            DATA_ACTION="keep"
            shift
            ;;
        --purge-data)
            DATA_ACTION="purge"
            shift
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

log_header "Lake of Tears — Teardown & Uninstall"

# Prerequisite verification
if ! command -v helm >/dev/null 2>&1 || ! command -v kubectl >/dev/null 2>&1; then
    log_error "Both 'helm' and 'kubectl' must be installed and accessible."
    exit 1
fi

# Determine data retention action
if [[ -z "${DATA_ACTION}" ]]; then
    if [[ -t 0 && "${FORCE}" != "true" ]]; then
        echo -e "${YELLOW}Persistent Volume Data Retention:${RESET}"
        echo "  1) Keep persistent data (PVCs preserved; can re-attach on future install)"
        echo "  2) Purge persistent data (IRREVERSIBLE: deletes MinIO buckets, PostgreSQL tables, notebooks)"
        read -r -p "Select action [1]: " choice
        case "${choice}" in
            2|"purge"|"Purge")
                DATA_ACTION="purge"
                ;;
            *)
                DATA_ACTION="keep"
                ;;
        esac
    else
        # Default safe behavior
        DATA_ACTION="keep"
    fi
fi

# Explicit confirmation safeguard if purging data
if [[ "${DATA_ACTION}" == "purge" && "${FORCE}" != "true" ]]; then
    if [[ -t 0 ]]; then
        echo -e "\n${BOLD}${RED}⚠️  DANGER: PERMANENT DATA LOSS WARNING ⚠️${RESET}"
        echo -e "You have requested to delete all PersistentVolumeClaims in namespace '${BOLD}${NAMESPACE}${RESET}'."
        echo -e "This will permanently erase all databases, object storage, and user notebooks.\n"
        read -r -p "Type '${NAMESPACE}' to confirm destruction: " typed_confirm
        if [[ "${typed_confirm}" != "${NAMESPACE}" ]]; then
            log_error "Confirmation string '${typed_confirm}' did not match '${NAMESPACE}'. Aborting teardown."
            exit 1
        fi
    else
        log_error "Purging data in a non-interactive shell requires the --force flag."
        exit 1
    fi
fi

# 1. Uninstall Helm release
log_info "Uninstalling Helm release 'lake-of-tears' in namespace '${NAMESPACE}'..."
if helm status lake-of-tears -n "${NAMESPACE}" >/dev/null 2>&1; then
    helm uninstall lake-of-tears -n "${NAMESPACE}"
    log_success "Helm release 'lake-of-tears' uninstalled."
else
    log_warn "Helm release 'lake-of-tears' not found in namespace '${NAMESPACE}'. Skipping Helm uninstall."
fi

# 2. Handle PersistentVolumeClaims and Namespace
if [[ "${DATA_ACTION}" == "purge" ]]; then
    log_info "Purging PersistentVolumeClaims in namespace '${NAMESPACE}'..."
    kubectl delete pvc --all -n "${NAMESPACE}" --wait=false 2>/dev/null || true

    log_info "Removing namespace '${NAMESPACE}'..."
    kubectl delete namespace "${NAMESPACE}" --wait=false 2>/dev/null || true
    log_success "Persistent data and namespace '${NAMESPACE}' scheduled for termination."
else
    log_success "Persistent data retained. PersistentVolumeClaims in namespace '${NAMESPACE}' remain intact for future deployments."
    log_info "To clean up remaining PVCs later, run: kubectl delete pvc --all -n ${NAMESPACE}"
fi

echo -e "\n${BOLD}${GREEN}Teardown complete.${RESET}\n"
