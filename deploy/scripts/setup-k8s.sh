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

DEPLOY_USER="${SUDO_USER:-openclaw}"

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

# ── Helm ─────────────────────────────────────────────────────────────────────
echo "==> Installing Helm"
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# ── Secrets directory ─────────────────────────────────────────────────────────
echo "==> Creating /etc/lake-of-tears/values-secret.yaml"
sudo mkdir -p /etc/lake-of-tears
sudo chown "${DEPLOY_USER}:${DEPLOY_USER}" /etc/lake-of-tears

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

gemini:
  apiKey: ""

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

echo ""
echo "==> Setup complete!"
echo ""
echo "NEXT STEPS:"
echo "  1. Edit /etc/lake-of-tears/values-secret.yaml with your real credentials"
echo "  2. Add your Jenkins agent public key to authorized_keys:"
echo "       echo 'PASTE_JENKINS_PUBLIC_KEY_HERE' >> ~/.ssh/authorized_keys"
echo "  3. In Jenkins: add a 'SSH Username with private key' credential"
echo "       ID:       vm-deploy-key"
echo "       Username: openclaw"
echo "       Key:      (paste the private key that pairs with the public key above)"
echo ""
echo "  Once deployed, reach Lake of Tears at: http://lake.10.0.0.60.nip.io"
echo ""
