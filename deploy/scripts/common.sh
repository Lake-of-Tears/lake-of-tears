#!/usr/bin/env bash
# =============================================================================
# Lake of Tears — Shared Lifecycle Shell Utilities
# =============================================================================

# Determine directory paths
SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -P "${SCRIPT_DIR}/../.." && pwd)"

# Color formatting
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    BOLD="\033[1m"
    DIM="\033[2m"
    RED="\033[0;31m"
    GREEN="\033[0;32m"
    YELLOW="\033[0;33m"
    BLUE="\033[0;34m"
    CYAN="\033[0;36m"
    RESET="\033[0m"
else
    BOLD=""
    DIM=""
    RED=""
    GREEN=""
    YELLOW=""
    BLUE=""
    CYAN=""
    RESET=""
fi

log_info() {
    echo -e "${CYAN}[INFO]${RESET} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${RESET} ${BOLD}$*${RESET}"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${RESET} $*" >&2
}

log_error() {
    echo -e "${RED}[ERROR]${RESET} ${BOLD}$*${RESET}" >&2
}

log_header() {
    echo -e "\n${BOLD}${BLUE}=== $* ===${RESET}\n"
}

# Resolve a Python environment with PyYAML and lifecycle modules available
resolve_python() {
    export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"

    # 1. Active virtualenv
    if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
        if "${VIRTUAL_ENV}/bin/python" -c "import yaml; import pipeline.shared.lifecycle" >/dev/null 2>&1; then
            echo "${VIRTUAL_ENV}/bin/python"
            return 0
        fi
    fi

    # 2. Local .venv in REPO_ROOT
    if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
        if "${REPO_ROOT}/.venv/bin/python" -c "import yaml; import pipeline.shared.lifecycle" >/dev/null 2>&1; then
            echo "${REPO_ROOT}/.venv/bin/python"
            return 0
        fi
    fi

    # 3. uv run python
    if command -v uv >/dev/null 2>&1; then
        if uv run python -c "import yaml; import pipeline.shared.lifecycle" >/dev/null 2>&1; then
            echo "uv run python"
            return 0
        fi
    fi

    # 4. System python3
    if command -v python3 >/dev/null 2>&1; then
        if python3 -c "import yaml; import pipeline.shared.lifecycle" >/dev/null 2>&1; then
            echo "python3"
            return 0
        fi
    fi

    # 5. System python
    if command -v python >/dev/null 2>&1; then
        if python -c "import yaml; import pipeline.shared.lifecycle" >/dev/null 2>&1; then
            echo "python"
            return 0
        fi
    fi

    return 1
}

ensure_python_runtime() {
    if LOT_PYTHON="$(resolve_python)"; then
        export LOT_PYTHON
        return 0
    fi

    log_warn "Python 3 is present but PyYAML is missing, which is required to parse TOON configurations."
    if [[ -t 0 ]]; then
        read -r -p "Install PyYAML now via pip/uv? [Y/n] " ans
        case "${ans}" in
            [yY][eE][sS]|[yY]|"")
                if command -v uv >/dev/null 2>&1; then
                    log_info "Installing PyYAML via uv..."
                    uv pip install pyyaml || pip install pyyaml
                elif command -v pip3 >/dev/null 2>&1; then
                    pip3 install pyyaml
                elif command -v pip >/dev/null 2>&1; then
                    pip install pyyaml
                fi
                ;;
            *)
                log_error "PyYAML is required. Please install PyYAML and try again."
                return 1
                ;;
        esac
        if LOT_PYTHON="$(resolve_python)"; then
            export LOT_PYTHON
            return 0
        fi
    fi

    log_error "Failed to locate Python with PyYAML. Please run 'pip install pyyaml' or activate a virtualenv."
    return 1
}

# Verify required CLI tools and cluster connectivity
check_prerequisites() {
    log_info "Verifying deployment prerequisites..."

    # Check kubectl
    if ! command -v kubectl >/dev/null 2>&1; then
        log_error "kubectl is not installed or not in PATH. Please install kubectl."
        return 1
    fi

    # Check helm
    if ! command -v helm >/dev/null 2>&1; then
        log_error "helm is not installed or not in PATH. Please install Helm 3."
        return 1
    fi

    # Check Python runtime
    ensure_python_runtime || return 1

    # Check Kubernetes cluster access
    if ! kubectl cluster-info >/dev/null 2>&1; then
        log_error "Cannot connect to Kubernetes cluster. Ensure KUBECONFIG is set or your cluster is running."
        return 1
    fi

    log_success "All prerequisites satisfied (kubectl, helm, cluster connectivity, python)."
    return 0
}

# Resolve TOON configuration path
resolve_config() {
    local explicit="${1:-}"
    ensure_python_runtime || return 1

    local resolved
    if resolved="$(${LOT_PYTHON} "${REPO_ROOT}/scripts/lifecycle_helper.py" find-config ${explicit:+-c "$explicit"} 2>/dev/null)"; then
        echo "${resolved}"
        return 0
    fi

    # If no config was found and we're interactive, offer to copy example
    if [[ -t 0 ]]; then
        log_warn "No Lake of Tears configuration file found."
        read -r -p "Create '${REPO_ROOT}/lake-of-tears.toon' from example now? [Y/n] " ans
        case "${ans}" in
            [yY][eE][sS]|[yY]|"")
                cp "${REPO_ROOT}/lake-of-tears.example.toon" "${REPO_ROOT}/lake-of-tears.toon"
                log_success "Created ${REPO_ROOT}/lake-of-tears.toon"
                echo "${REPO_ROOT}/lake-of-tears.toon"
                return 0
                ;;
            *)
                ;;
        esac
    fi

    log_error "No configuration found. Please copy lake-of-tears.example.toon to lake-of-tears.toon or pass --config <path>."
    return 1
}

# Validate secrets and handle auto-generation
handle_secrets() {
    local config_file="$1"
    local auto_gen="${2:-false}"
    local force="${3:-false}"

    ensure_python_runtime || return 1

    local placeholders
    placeholders="$(${LOT_PYTHON} "${REPO_ROOT}/scripts/lifecycle_helper.py" scan-placeholders -i "${config_file}" 2>/dev/null)" || true

    if [[ -n "${placeholders}" ]]; then
        if [[ "${auto_gen}" == "true" ]]; then
            log_info "Auto-generating secure random credentials for placeholder secrets..."
            ${LOT_PYTHON} "${REPO_ROOT}/scripts/lifecycle_helper.py" generate-secrets -i "${config_file}" --in-place
            log_success "Secrets populated in ${config_file} (backup saved to ${config_file}.bak)."
            return 0
        fi

        if [[ -t 0 ]]; then
            log_warn "Configuration contains default placeholder credentials (changeme-*):"
            echo -e "${DIM}${placeholders}${RESET}"
            read -r -p "Auto-generate cryptographically secure random secrets now? [Y/n] " ans
            case "${ans}" in
                [yY][eE][sS]|[yY]|"")
                    ${LOT_PYTHON} "${REPO_ROOT}/scripts/lifecycle_helper.py" generate-secrets -i "${config_file}" --in-place
                    log_success "Secrets populated in ${config_file} (backup saved to ${config_file}.bak)."
                    return 0
                    ;;
                *)
                    if [[ "${force}" != "true" ]]; then
                        log_warn "Continuing with unedited placeholder credentials..."
                    fi
                    ;;
            esac
        elif [[ "${force}" != "true" ]]; then
            log_error "Configuration contains default placeholder credentials. Pass --auto-generate-secrets to populate automatically or --force to bypass."
            return 1
        fi
    fi

    return 0
}

# Poll a health URL until ready or timeout
poll_health() {
    local url="$1"
    local timeout="${2:-60}"
    local start_time
    start_time="$(date +%s)"

    log_info "Polling health endpoint: ${url}"
    while true; do
        if curl -kfsSL "${url}" >/dev/null 2>&1; then
            log_success "Health check passed!"
            return 0
        fi

        local now
        now="$(date +%s)"
        if (( now - start_time >= timeout )); then
            log_warn "Health check timed out after ${timeout}s. Services may still be completing startup."
            return 1
        fi
        sleep 3
    done
}
