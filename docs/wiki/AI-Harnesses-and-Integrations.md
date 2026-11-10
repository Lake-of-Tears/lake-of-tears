# AI Harnesses, RAG Gateways & MCP Integrations

Lake of Tears provides a unified, permission-aware AI gateway and native Model Context Protocol (MCP) server so external conversational harnesses (**Claude Desktop**, **Cursor**, **Windsurf**, **Antigravity**, **Open WebUI**, **AnythingLLM**) and programmatic agent frameworks (**LangChain / LangGraph**, **LlamaIndex**, **CrewAI**, **OpenAI Agents SDK**) can query datalake vector embeddings and catalog tables while strictly enforcing per-user RBAC.

---

## 1. Architecture Overview

External AI harnesses can integrate with Lake of Tears through three complementary standards:

1. **OpenAI-Compatible Gateway (`/api/v1/models`, `/api/v1/chat/completions`, `/api/v1/embeddings`)**
   - Drop-in OpenAI-compatible API exposing virtual models:
     - `lot-rag` / `lot-rag:<workspace_slug>` — Semantic vector search over authorized Parquet vector indices + grounded LLM synthesis.
     - `lot-genie` / `lot-genie:<workspace_slug>` — Autonomous Text-to-SQL synthesis, DuckDB execution, 1-step self-correction, and narrative insight.
     - `lot-hybrid` — Combined DuckDB SQL + semantic vector RAG.
2. **Permission-Aware Vector Retrieval API (`/api/v1/retrieve` & `/api/v1/vector_stores/{id}/search`)**
   - Returns raw, RBAC-filtered document chunks (`page_content`, `score`, `metadata`) without LLM generation so external harnesses can run their own prompt templates, rerankers, or multi-source RAG pipelines.
   - Implements the **OpenAI Vector Store Search API** (`GET /api/v1/vector_stores`, `POST /api/v1/vector_stores/{vector_store_id}/search`) for native OpenAI `file_search` / Responses API compatibility.
3. **Native MCP Server (`/api/mcp` & `/api/v1/mcp`)**
   - JSON-RPC 2.0 Model Context Protocol endpoint (Streamable HTTP `POST /api/mcp` and SSE `GET /api/mcp/sse`) exposing 5 RBAC-scoped tools (`list_workspaces_and_catalogs`, `describe_table`, `semantic_search_datalake`, `query_datalake_sql`, `ask_genie`) and catalog resources (`lot://catalogs/{workspace_slug}/{catalog_slug}`).

---

## 2. Role-Based Access Control (Admin vs. Scoped Worker)

Every request to `/api/v1/*` and `/api/mcp` resolves the caller's effective Lake of Tears identity and enforces three-layer isolation:

| Persona | Lake of Tears Role / Membership | Accessible Vector Indices & Tables | Behavior on Restricted Assets |
| :--- | :--- | :--- | :--- |
| **Platform Admin** | `role="superadmin"` | All active workspaces, catalogs, schemas, and tables across the lakehouse (`lot-rag`, `lot-genie`, `vector_stores/all`). | Full read access across all workspaces. |
| **Scoped Worker (e.g. Training User)** | `role="user"`, member of `training` workspace (or approved `CatalogAccess` share) | Only `training` workspace embeddings and explicitly approved shared catalogs (`lot-rag:training`). | Never sees restricted models in `/api/v1/models`; receives `403 Forbidden` if querying `executive` workspace/catalog/tables. |
| **Trusted Proxy Impersonation** | Admin Service PAT + `X-On-Behalf-Of: worker@company.com` | Automatically downgraded to `worker@company.com`'s exact workspace memberships and `CatalogAccess` grants. | Identical to the Worker's own PAT (`403 Forbidden` on unauthorized workspaces/catalogs). |

### Setting Up Admin vs. Training-Only Worker Access

1. **Create the Training Workspace & Catalog:**
   - In Lake of Tears, create workspace `training` and catalog `training` (e.g. `training.gold.onboarding_docs`).
   - Enable the **Vector Index** on `training.gold.onboarding_docs` in the Catalog Explorer drawer so embeddings are synced to MinIO Parquet.
2. **Assign User Permissions:**
   - Add your Admin account with `superadmin` role (or workspace `admin` across all workspaces).
   - Add your Worker account (`worker@company.com`) **only** to the `training` workspace with `read` role (or grant an approved `CatalogAccess` share on the `training` catalog).
3. **Authenticate from Your Harness:**
   - **Option A (Per-User PAT):** Each user generates a Personal Access Token (`lot_pat_...`) under **Account Settings -> Personal Access Tokens** and configures it in their AI client.
   - **Option B (Automatic SSO Identity Pass-Through):** Configure your AI proxy or harness with a single `superadmin` Service PAT and pass `X-On-Behalf-Of: "<user_email>"` in `headers`.

---

## 3. Standard OpenAI & MCP Client Configuration

### 3.1 Custom OpenAI-Compatible Endpoint (SSO Identity Pass-Through or User PAT)

Any OpenAI-compatible client or multi-user AI gateway can connect to `/api/v1`:

```yaml
endpoints:
  custom:
    # Option A: Automatic Per-User RBAC via Trusted Proxy Header (Recommended for SSO teams)
    - name: "Lake of Tears"
      apiKey: "${LOT_ADMIN_SERVICE_PAT}"
      baseURL: "http://lake-of-tears-backend:8000/api/v1"
      headers:
        X-On-Behalf-Of: "${USER_EMAIL}"
      models:
        default:
          - "lot-rag"
          - "lot-genie"
          - "lot-hybrid"
        fetch: true

    # Option B: Per-User Personal Access Token
    - name: "Lake of Tears (Personal Token)"
      apiKey: "${LOT_USER_PAT}"
      baseURL: "http://lake-of-tears-backend:8000/api/v1"
      models:
        default:
          - "lot-rag"
          - "lot-genie"
          - "lot-hybrid"
        fetch: true
```

### 3.2 Native Remote MCP Server Configuration

To let MCP clients (e.g., Claude Desktop, Antigravity, Cursor, or custom agents) invoke Lake of Tears tools (`semantic_search_datalake`, `query_datalake_sql`, `describe_table`, `list_workspaces_and_catalogs`, `ask_genie`) with per-user RBAC:

```yaml
mcpServers:
  lake-of-tears:
    type: streamable-http
    url: "http://lake-of-tears-backend:8000/api/mcp"
    headers:
      Authorization: "Bearer ${LOT_ADMIN_SERVICE_PAT}"
      X-On-Behalf-Of: "${USER_EMAIL}"
    timeout: 60000
```

---

## 4. Python SDK (`lake_of_tears.ai` / `lot.ai`)

The Lake of Tears Python SDK provides `LakeOfTearsClient`, `LakeOfTearsRetriever`, and `LakeOfTearsToolkit` in both `lake_of_tears` and `lot`.

### 4.1 LangChain / LangGraph Retriever with Per-User RBAC

```python
from lot import LakeOfTearsClient, LakeOfTearsRetriever

# Initialize with a superadmin service PAT (or individual user PAT)
client = LakeOfTearsClient(
    base_url="http://localhost:8000",
    api_key="lot_pat_...",
)

retriever = LakeOfTearsRetriever(client=client, top_k=5)

# 1. Query as Admin (retrieves across all workspaces/catalogs)
admin_docs = retriever.invoke("What M&A targets and onboarding modules exist?")
for doc in admin_docs:
    print(doc.metadata["source"], doc.score, doc.page_content)

# 2. Query on behalf of a Training Worker (strictly scoped to 'training' workspace/catalogs)
worker_docs = retriever.invoke(
    "What onboarding modules exist?",
    config={"configurable": {"on_behalf_of": "worker@company.com"}},
)
```

### 4.2 LangGraph / CrewAI / OpenAI Agents Toolkit

```python
from lot import LakeOfTearsToolkit

# Scoped toolkit for a specific user
toolkit = LakeOfTearsToolkit(
    base_url="http://localhost:8000",
    api_key="lot_pat_...",
    on_behalf_of="worker@company.com",
)

# Ready-to-bind tool functions: [search_datalake, query_sql, list_tables, ask_genie]
tools = toolkit.get_tools()

# Or invoke directly:
tables = toolkit.list_tables()
chunks = toolkit.search_datalake("warehouse safety orientation", top_k=3)
sql_result = toolkit.query_sql("SELECT source, content FROM training.gold.onboarding_docs LIMIT 5")
```

### 4.3 LlamaIndex Retriever Usage

```python
from lot import LakeOfTearsRetriever

retriever = LakeOfTearsRetriever(
    base_url="http://localhost:8000",
    api_key="lot_pat_...",
    workspace_slug="training",
    catalog="training",
    top_k=5,
)

nodes = retriever.retrieve("forklift inspection checklist")
for node in nodes:
    print(node.text, node.score, node.metadata)
```

### 4.4 Standard OpenAI Python SDK (`openai.OpenAI`)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/api/v1",
    api_key="lot_pat_...",
    default_headers={"X-On-Behalf-Of": "worker@company.com"},
)

# List RBAC-filtered models
models = client.models.list()

# Grounded RAG chat completion scoped to 'training'
response = client.chat.completions.create(
    model="lot-rag:training",
    messages=[{"role": "user", "content": "Summarize the warehouse safety orientation."}],
)
print(response.choices[0].message.content)
```
