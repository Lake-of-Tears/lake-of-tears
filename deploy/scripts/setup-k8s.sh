#!/usr/bin/env bash
# setup-k8s.sh — Run once on 10.0.0.60 as openclaw to prepare the VM for Lake of Tears.
#
# Usage:
#   ssh openclaw@10.0.0.60
#   curl -fsSL https://raw.githubusercontent.com/Lake-of-Tears/lake-of-tears/main/deploy/scripts/setup-k8s.sh | bash
#   # OR copy this file and run: bash setup-k8s.sh
#
# After this script finishes:
#   1. Edit /etc/lake-of-tears/values-secret.yaml with your real credentials
#   2. Add your Jenkins agent SSH public key (see bottom of script output)
#   3. Create a Jenkins "SSH Username with private key" credential (ID: vm-deploy-key)

set -euo pipefail

DEPLOY_USER="${SUDO_USER:-$(whoami)}"

echo "==> Installing system prerequisites"
sudo apt-get update -q
sudo apt-get install -y -q curl git ca-certificates gnupg

# ── Docker ──────────────────────────────────────────────────────────────────
echo "==> Installing Docker"
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "${DEPLOY_USER}"
echo "NOTE: log out and back in for docker group to take effect, or run: newgrp docker"

# ── k3s (Traefik disabled — we use nginx ingress instead) ───────────────────
echo "==> Installing k3s"
curl -sfL https://get.k3s.io | sh -s - --disable=traefik

# Wait for k3s to be ready
echo "==> Waiting for k3s node to be Ready..."
until sudo k3s kubectl get node | grep -q " Ready"; do sleep 3; done

# Give deploy user kubectl access
mkdir -p "/home/${DEPLOY_USER}/.kube"
sudo cp /etc/rancher/k3s/k3s.yaml "/home/${DEPLOY_USER}/.kube/config"
sudo chown "${DEPLOY_USER}:${DEPLOY_USER}" "/home/${DEPLOY_USER}/.kube/config"
sudo chmod 600 "/home/${DEPLOY_USER}/.kube/config"
grep -qxF 'export KUBECONFIG=~/.kube/config' "/home/${DEPLOY_USER}/.bashrc" \
    || echo 'export KUBECONFIG=~/.kube/config' >> "/home/${DEPLOY_USER}/.bashrc"

# Allow deploy user to import images into k3s containerd without a password prompt
echo "${DEPLOY_USER} ALL=(root) NOPASSWD: /usr/local/bin/k3s ctr images import *" \
    | sudo tee /etc/sudoers.d/k3s-ctr-import > /dev/null

# ── nginx Ingress Controller ─────────────────────────────────────────────────
echo "==> Installing nginx Ingress Controller"
# Uses LoadBalancer type; k3s ServiceLB (klipper) binds it to port 80/443 on the host.
NGINX_VERSION="v1.10.1"
sudo k3s kubectl apply -f \
    "https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-${NGINX_VERSION}/deploy/static/provider/cloud/deploy.yaml"

echo "==> Waiting for ingress-nginx controller to be ready..."
sudo k3s kubectl rollout status deployment ingress-nginx-controller \
    -n ingress-nginx --timeout=120s

# ── Lake of Tears Namespace ──────────────────────────────────────────────────
echo "==> Ensuring lake-of-tears namespace exists"
sudo k3s kubectl create namespace lake-of-tears --dry-run=client -o yaml | sudo k3s kubectl apply -f -

# ── Helm ─────────────────────────────────────────────────────────────────────
echo "==> Installing Helm"
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# ── Configuration & Secrets directory ───────────────────────────────────────
echo "==> Setting up /etc/lake-of-tears directory"
sudo mkdir -p /etc/lake-of-tears
sudo chown "${DEPLOY_USER}:${DEPLOY_USER}" /etc/lake-of-tears

if [ ! -f /etc/lake-of-tears/values-prod.yaml ]; then
    echo "==> Creating /etc/lake-of-tears/values-prod.yaml"
    cat > /etc/lake-of-tears/values-prod.yaml << 'EOF'
# /etc/lake-of-tears/values-prod.yaml
# Production overrides for Lake of Tears.
# Lives on the host VM and is never committed to git.

global:
  storageClass: local-path

ingress:
  enabled: true
  className: nginx
  host: app.lakeoftears.com
  minioConsoleHost: minio.lakeoftears.com
  minioApiHost: s3.lakeoftears.com
  supersetHost: superset.lakeoftears.com

lakeui:
  minioConsoleUrl: "https://minio.lakeoftears.com"
  supersetUrl: "https://superset.lakeoftears.com"
  resources:
    requests:
      cpu: "100m"
      memory: "256Mi"
    limits:
      cpu: "500m"
      memory: "512Mi"

minio:
  persistence:
    size: 150Gi
  resources:
    requests:
      cpu: "500m"
      memory: "1Gi"
    limits:
      cpu: "2"
      memory: "4Gi"

jupyter:
  persistence:
    size: 20Gi
  resources:
    requests:
      cpu: "250m"
      memory: "1Gi"
    limits:
      cpu: "2"
      memory: "4Gi"

superset:
  persistence:
    size: 5Gi
  resources:
    requests:
      cpu: "250m"
      memory: "1Gi"
    limits:
      cpu: "1"
      memory: "2Gi"

airflow:
  persistence:
    size: 10Gi
  resources:
    requests:
      cpu: "500m"
      memory: "2Gi"
    limits:
      cpu: "2"
      memory: "4Gi"

backend:
  resources:
    requests:
      cpu: "100m"
      memory: "256Mi"
    limits:
      cpu: "1"
      memory: "512Mi"

postgres:
  persistence:
    size: 20Gi
  resources:
    requests:
      cpu: "100m"
      memory: "256Mi"
    limits:
      cpu: "1"
      memory: "1Gi"
EOF
fi

if [ ! -f /etc/lake-of-tears/values-secret.yaml ]; then
    echo "==> Creating /etc/lake-of-tears/values-secret.yaml"
    cat > /etc/lake-of-tears/values-secret.yaml << 'EOF'
# /etc/lake-of-tears/values-secret.yaml
# Secrets for the Lake of Tears Helm deployment.
# This file lives ONLY on the VM and is never committed to git.

minio:
  rootUser: "minio"
  rootPassword: "changeme-minio"

jupyter:
  token: "changeme-jupyter"

superset:
  secretKey: "changeme-superset-secret"
  adminPassword: "changeme-superset-admin"

airflow:
  secretKey: "changeme-airflow-secret"
  adminPassword: "changeme-airflow-admin"

stripe:
  secretKey: ""

shopify:
  storeDomain: ""
  accessToken: ""

hubspot:
  accessToken: ""

postgres:
  dsn: ""
EOF
fi

echo ""
echo "==> Setup complete!"
echo ""
echo "NEXT STEPS:"
echo "  1. Review /etc/lake-of-tears/values-prod.yaml (hostnames and resources)"
echo "  2. Edit /etc/lake-of-tears/values-secret.yaml with real credentials"
echo "  3. Deploy Lake of Tears via Helm:"
echo "       helm upgrade --install lake-of-tears ./deploy/helm/lake-of-tears \\"
echo "         --namespace lake-of-tears \\"
echo "         --create-namespace \\"
echo "         -f /etc/lake-of-tears/values-prod.yaml \\"
echo "         -f /etc/lake-of-tears/values-secret.yaml"
echo ""
echo "  Verify deployment status:"
echo "       kubectl get pods -n lake-of-tears"
echo ""
