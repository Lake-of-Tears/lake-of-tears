# 0003: Secret Management Architecture and External Vault Integrations

- **Status:** Accepted and implemented across tickets #380, #381, #382, #383.
- **Tickets:** #380 (Core Architecture, Models & Internal Vault), #381 (Azure Key Vault & AWS Secrets Manager Providers), #382 (Secret Consumption Runtime, SDK & Automatic Log Redaction), #383 (Superadmin Configuration Panel & Workspace Scopes UI), #384 (Phase 2 External Providers: HashiCorp Vault & Google Cloud Secret Manager).
- **Date:** 2026-11-05

## Context

Prior to this architecture, credentials such as API keys, database connection strings, and webhook tokens used in notebooks and pipelines had to be supplied through global environment variables or plain text config files. This presented several operational and security limitations:
1. **Security & Compliance:** Plaintext credentials stored in notebooks or Git repositories risked unauthorized exposure and accidental leakage into execution logs.
2. **Databricks Migration Barrier:** Teams migrating notebooks and workflows from Databricks rely heavily on the `dbutils.secrets` SDK (`secrets.get(scope, key)`) and scope-based permissions.
3. **Enterprise Vault Requirements:** Enterprise platforms require integrations with central cloud key management systems (such as Azure Key Vault and AWS Secrets Manager) rather than maintaining duplicate credentials across tools.
4. **Tenant Isolation:** Teams within different workspaces required isolated secret namespaces with role-based access control (RBAC).

To solve these requirements, Lake of Tears implemented a unified **Secret Management Architecture**.

---

## Decision 1: Scope-Based Namespace with 3-Tier ACLs

**Chosen: Databricks-compatible Secret Scopes with `READ`, `WRITE`, `MANAGE` access control levels.**
Secrets are organized into **Scopes**. Each scope belongs to a workspace and enforces a 3-tier permission model:
- `READ`: Resolve secret values in code and pipeline configs; inspect scope metadata.
- `WRITE`: Add, rotate, and delete keys within the scope.
- `MANAGE`: Full ownership; grant/revoke ACLs to workspace users and roles; delete the scope.

| | Chosen: Databricks 3-Tier Scope ACLs | Alternative: Global Flat Key-Value Store |
|---|---|---|
| Idea | Hierarchical namespaces with granular user/role permission grants per scope | Single flat table of key-value pairs without scopes or permission delegation |
| For | 100% Databricks parity; fine-grained least-privilege security; workspace scoping | Simpler database schema |
| Against | Requires ACL resolution middleware and principal lookup | Inability to restrict access per team or workflow; cannot mirror enterprise secret boundaries |
| To revert | Drop `secret_acls` table and remove permission checks from `SecretManagerService`. | n/a |

---

## Decision 2: Multi-Provider Vault Abstraction with Internal Default

**Chosen: Pluggable `BaseSecretProvider` supporting Internal AES-256-GCM Vault, Azure Key Vault, and AWS Secrets Manager.**
Lake of Tears provides an internal encrypted vault out of the box so that self-hosted environments require zero external cloud setup, while simultaneously allowing enterprise deployments to delegate secret storage to Azure Key Vault or AWS Secrets Manager.

1. **`InternalSecretProvider`**: Uses AES-256-GCM authenticated encryption with unique per-secret IVs and master salt HMAC integrity checking. Plaintext values are never stored and never exposed in list responses.
2. **`AzureKeyVaultProvider`**: Interacts with Azure Key Vault using `DefaultAzureCredential` (supporting Azure Workload Identity / AKS pod identity) or Service Principal credentials (`ClientSecretCredential`).
3. **`AWSSecretsManagerProvider`**: Interacts with AWS Secrets Manager using standard AWS SDK credential chains (supporting EKS IRSA, IAM instance profiles, STS AssumeRole, or explicit access keys).

| | Chosen: Pluggable provider abstraction with internal fallback | Alternative: Cloud-only (External KMS required) |
|---|---|---|
| Idea | Support local encrypted storage alongside cloud key vaults via a common interface | Require users to bring an external cloud vault (Azure or AWS) to use secrets |
| For | Out-of-the-box functionality for local development, on-premise Kubernetes, and air-gapped clusters | Offloads all cryptographic storage concerns to cloud hyperscalers |
| Against | Requires maintaining database envelope encryption logic | Breaks local self-hosted and zero-cloud installations |
| To revert | Remove `InternalSecretProvider` and make cloud provider configuration mandatory in Helm values. | n/a |

---

## Decision 3: Databricks SDK Parity and Configuration Templating

**Chosen: Native Python SDK (`lake_of_tears.secrets` / `lot.secrets`) and configuration interpolation syntax.**
To provide an effortless developer experience and direct parity with Databricks:
- Python developers use `secrets.get(scope, key)`, `secrets.list(scope)`, and `secrets.list_scopes()`.
- Pipeline authors use template placeholders: `{{secrets/scope/key}}`, `{{secrets.scope.key}}`, and `${secrets:scope:key}` inside YAML configs and Airflow DAGs.

Placeholders are resolved recursively at runtime before task execution, verifying user permissions on each lookup.

---

## Decision 4: Automatic Real-Time Output Redaction

**Chosen: In-memory thread-safe secret registry and stream wrappers (`SecretRedactionFilter` / `RedactingStream`).**
Whenever a secret is resolved by the SDK or template interpolator:
1. The plaintext secret value is registered in a thread-safe redaction registry.
2. Any log output emitted via Python `logging`, `sys.stdout`, or `sys.stderr` is intercepted.
3. Registered secrets are substituted with `[REDACTED]` before writing to logs, Airflow task instance execution logs, or terminal streams.

This eliminates accidental credential leakage in logs while preserving error context and debug traces.
