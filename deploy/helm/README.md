# Lake of Tears Helm Chart & TOON Deployment Guide

This directory contains the production-ready Helm chart (`./lake-of-tears`) and configuration templates for orchestrating the complete Lake of Tears data lakehouse platform on Kubernetes (k3s, managed K8s, EKS, GKE, AKS).

---

## TOON-First Configuration

Lake of Tears provides a unified, token-dense configuration template in **Token-Oriented Object Notation (TOON)** located at the repository root: [`lake-of-tears.example.toon`](../../lake-of-tears.example.toon).

TOON reduces configuration token counts by 40–60% compared to YAML, making it ideal for LLM-driven DevOps agents, CI/CD automation, and clean human maintenance.

### Compiling TOON to Helm `values.yaml`

Use the included utility script [`scripts/toon2helm.py`](../../scripts/toon2helm.py) to compile your `.toon` file into Helm-compatible values:

```bash
# 1. Copy the example specification
cp lake-of-tears.example.toon /etc/lake-of-tears/lake-of-tears.toon

# 2. Compile to values.yaml
python scripts/toon2helm.py -i /etc/lake-of-tears/lake-of-tears.toon -o /etc/lake-of-tears/values-prod.yaml
```

### On-the-Fly Deployment (Without Writing Secrets to Disk)

You can stream the compiled YAML directly to `helm upgrade --install` via process substitution:

```bash
helm upgrade --install lake-of-tears ./deploy/helm/lake-of-tears \
  --namespace lake-of-tears \
  --create-namespace \
  -f <(python scripts/toon2helm.py -i /etc/lake-of-tears/lake-of-tears.toon) \
  --wait --timeout 10m
```

---

## Configuration Mapping Reference

| TOON Section | Helm Chart Path | Description |
|---|---|---|
| `global.storageClass` | `global.storageClass` | PVC storage class (`local-path`, NFS, cloud CSI) |
| `ingress.*` | `ingress.*` | External hostnames, nginx ingress annotations, TLS secrets |
| `minio.resources` | `minio.resources.{requests,limits}` | Normalized CPU and RAM budgets for MinIO |
| `minio.buckets` | `minio.buckets` | Buckets automatically created during `minio-init` job |
| `lakeui.resources` | `lakeui.resources.{requests,limits}` | Resource allocations for the unified FastAPI shell |
| `jupyter.persistence` | `jupyter.persistence.size` | PVC disk size for persistent Jupyter notebooks |
| `superset.secretKey` | `superset.secretKey` | Flask secret key for Superset BI |
| `airflow.waitForDb` | `airflow.waitForDb` | Wait container ensuring Postgres DB readiness on startup |
| `backend.oauth[...]` | `backend.{google,github,...}` | OAuth/OIDC tabular rows unpacked to Helm auth providers |
| `connectors[...]` | `connectors` | Tabular registry of active ingestion sources and schedules |
| `stripe`, `shopify`, etc. | `stripe.*`, `shopify.*` | Data connector API tokens and secret keys |

---

## Standard Helm Values Files

If you prefer static YAML overrides:
- `values-local.example.yaml`: Overrides for local development on k3s / minikube.
- `values-prod.example.yaml`: Production overrides with ingress hosts and persistent storage limits.
