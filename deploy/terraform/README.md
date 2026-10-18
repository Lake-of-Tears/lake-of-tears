# Terraform

Two independent modules, each with its own state:

| Module | Purpose |
|---|---|
| `foundation/` | TrueNAS ZFS dataset and snapshots, and the MinIO `datalake` bucket (provider `aminueza/minio`) |
| `services/` | Docker, scripts and Docker Compose on a VM. **Does not validate yet**, see [#227](https://github.com/Lake-of-Tears/lake-of-tears/issues/227) |

## Checks (also run in CI)

```bash
terraform fmt -check -recursive deploy/terraform/
cd deploy/terraform/foundation
terraform init -backend=false
terraform validate
```

CI runs the same commands with Terraform 1.6.6 on every pull request and push to `main`.

## Upgrading an existing environment

The `aminueza/minio` provider is pinned to `~> 3.45` (it was 2.x earlier). In a working copy that was initialised with the older provider, run this once before planning:

```bash
cd deploy/terraform/foundation
terraform init -upgrade
```
