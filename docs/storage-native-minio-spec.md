# Feature Specification: Native MinIO S3 API Integration & In-App Storage Browser UI (#109)

## 1. Executive Summary & Goal
Transition Lake of Tears storage management from an external iframe / link to the standalone MinIO Console (port 9001) into a first-class native Lake of Tears UI powered by an API-driven backend service.

This brings:
- Hierarchical S3 prefix and object exploration for `s3://datalake/`.
- Fast, low-overhead Parquet schema and metadata inspection (row counts, column types, row groups, compression codec) via PyArrow.
- Role-Based Access Control (RBAC): all workspace members can browse, download, and query; destructive actions (object deletion) require workspace `admin` or `superadmin`.
- Deep integration with DuckDB SQL Editor (`/sql?q=...`) for one-click querying of files and folder globs.
- Full theme compatibility (dark/light) matching Lake of Tears UI standards.
- A subtle superadmin-only escape hatch to the legacy MinIO Admin Console.

---

## 2. Architecture & Design Decisions (Resolved via /grill-me)

```
┌────────────────────────────────────────────────────────────────────────────┐
│                             Lake of Tears UI                               │
│                   (/storage, /sql?q=..., storage.html)                      │
└────────────────────────────────────┬───────────────────────────────────────┘
                                     │ (Forwarding Proxy)
                                     ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                    FastAPI Core Backend (storage_router.py)                 │
│         Auth & Workspace RBAC Context  •  Pagination Continuation          │
└───────────────────┬───────────────────────────────────┬────────────────────┘
                    │                                   │
                    ▼                                   ▼
┌──────────────────────────────────────┐  ┌──────────────────────────────────┐
│      storage_service.py              │  │      pyarrow.parquet             │
│  Boto3 S3 Client (list, head,        │  │  Streams Parquet footer range    │
│  delete, presigned URLs, streaming)  │  │  Extracts schema, rows, codecs   │
└───────────────────┬──────────────────┘  └──────────────────────────────────┘
                    │
                    ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                         MinIO S3 Object Store                              │
│                    Bucket: s3://datalake/ (Default)                        │
└────────────────────────────────────────────────────────────────────────────┘
```

| Decision Area | Selected Approach | Key Rationale |
| :--- | :--- | :--- |
| **Workspace Tenancy & RBAC** | **Unified Lake View with Action RBAC** | All workspace members can browse data lake prefixes (`raw/`, `embeddings/`, catalogs) to build queries, while object deletions require `admin` or `superadmin` role (viewers are read-only). |
| **Bucket Scope** | **Single-Bucket Lake Explorer (`datalake`)** | Strictly constrains browsing to the configured `MINIO_BUCKET` (`datalake`), maintaining data lake consistency and catalog alignment. |
| **Parquet Inspection Engine** | **PyArrow (`pyarrow.parquet`)** | Fetches and parses only the Parquet footer via HTTP byte range request. Zero database startup overhead, no offline extension download flakiness, and minimal memory footprint. |
| **Download & Write Actions** | **Presigned URLs with Streaming Fallback + Delete** | Generates secure presigned S3 URLs using `MINIO_PUBLIC_ENDPOINT` (or fallback streaming proxy if client cannot resolve MinIO DNS). Deletion enabled with confirmation modal for admins. Direct file upload deferred to future release. |
| **DuckDB Query Integration** | **Seamless SQL Editor Redirection + Prefix Globbing** | "Query with DuckDB" redirects to `/sql?q=...` pre-filling `SELECT * FROM read_parquet(...)`. Enhanced `/sql` route automatically populates the query text. Supports single file and folder recursive glob (`prefix/**/*.parquet`). |
| **UI Explorer Layout** | **Breadcrumb + Table Explorer with Details Modal** | Top breadcrumb navigation bar, quick search filter, responsive object list table with action buttons, and a rich Parquet details/schema modal. |
| **Pagination Strategy** | **Hybrid S3 Cursor Pagination** | Uses S3 continuation tokens with configurable page size (default 100), returning folders and files; UI provides Prev/Next paging with total count badge. |
| **Legacy MinIO Console Link** | **Superadmin-Only Escape Hatch** | Hidden for general users; displayed only to `superadmin` users as an advanced server administration escape hatch. |

---

## 3. Detailed Component Architecture

### 3.1 Backend Service (`backend/storage_service.py`)
- **Configuration**:
  - `MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")`
  - `MINIO_PUBLIC_ENDPOINT = os.getenv("MINIO_PUBLIC_ENDPOINT", "http://localhost:9000")`
  - `MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")`
  - `MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")`
  - `MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")`
  - `MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() in ("true", "1")`
- **Core Functions**:
  1. `browse_prefix(prefix: str = "", limit: int = 100, continuation_token: str | None = None)`:
     - Calls `s3_client.list_objects_v2(Bucket=MINIO_BUCKET, Prefix=prefix, Delimiter="/", MaxKeys=limit, ContinuationToken=...)`.
     - Returns `folders` (`CommonPrefixes`), `files` (`Contents`), `is_truncated`, `next_continuation_token`, and `current_prefix`.
  2. `get_object_metadata(key: str)`:
     - `s3_client.head_object` for content length, last modified, content type, ETag.
     - For `.parquet` files:
       - Uses `boto3` byte-range request to read the Parquet footer (or `pyarrow.parquet.ParquetFile`).
       - Extracts: column schemas (name, arrow_type, nullable), total row count, row group count, compression codecs (e.g., `SNAPPY`, `ZSTD`), created_by string.
  3. `get_download_url(key: str, expires_in: int = 3600)`:
     - Generates presigned S3 GET URL using `MINIO_PUBLIC_ENDPOINT`.
  4. `stream_object(key: str)`:
     - Streams `s3_client.get_object(Bucket=MINIO_BUCKET, Key=key)["Body"]`.
  5. `delete_object(key: str)`:
     - `s3_client.delete_object(Bucket=MINIO_BUCKET, Key=key)`.
  6. `get_bucket_summary()`:
     - Returns aggregated total objects, total size, top-level prefix breakdown, raw data, embeddings summary.

### 3.2 Backend Router (`backend/storage_router.py`)
- Prefix: `/api/v1/storage` and `/api/storage`
- Endpoints:
  - `GET /stats`: Overall bucket stats and prefix breakdown.
  - `GET /browse?prefix=...&limit=...&continuation_token=...`: List folders and files in prefix.
  - `GET /objects/{key:path}/metadata`: Parquet schema & file metadata.
  - `GET /objects/{key:path}/download`: Presigned download URL payload.
  - `GET /objects/{key:path}/stream`: Streaming download proxy.
  - `DELETE /objects/{key:path}`: Delete object. Checks user role: `viewer` gets 403; `editor`/`admin`/`superadmin` allowed.

### 3.3 UI Layer (`ui/main.py` & `ui/templates/storage.html`)
- **`ui/main.py`**:
  - Update `GET /sql`: extract `q` or `sql` query parameter and pass it into template context.
  - Proxy endpoints for `/api/v1/storage/...` forwarding to `BACKEND_URL`.
  - Storage page route `GET /storage`: renders `storage.html` passing initial bucket stats, current user role, and `is_superadmin`.
- **`ui/templates/storage.html`**:
  - Breadcrumb navigation: clickable folder trail (`datalake / raw / stripe_charges / ...`).
  - Search / Filter bar: instantaneous text filtering across current folder items.
  - Summary badges: item count, size, refresh button.
  - Explorer Table:
    - Folders: icon, name, action button ("Query with DuckDB" for `prefix/**/*.parquet`).
    - Files: icon (Parquet badge, notebook, generic), size, last modified.
    - Action buttons: "Inspect Schema", "Query with DuckDB", "Download", "Delete" (admin only).
  - Parquet Metadata Modal:
    - Summary metrics: total rows, row groups, format, size, last modified.
    - Columns schema table: name, type, nullable, compression.
    - Actions: "Query in SQL Editor", "Download", "Copy S3 URI".
  - Delete Confirmation Modal:
    - Safeguard modal displaying key and asking for confirmation.
  - Dark/Light Theme Support: full styling using existing Tailwind tokens.
  - Superadmin Escape Hatch: subtle "MinIO Admin Console" link visible only when `is_superadmin` is true.

---

## 4. Verification & Testing Strategy
1. **Backend Unit & Integration Tests (`tests/test_backend_storage.py`)**:
   - `test_browse_prefix_root`: Lists top-level folders (`raw/`, `embeddings/`, etc.).
   - `test_browse_prefix_nested_and_pagination`: Tests nested folders and continuation tokens.
   - `test_parquet_metadata_inspection`: Mock Parquet file with PyArrow; asserts schema, column types, row count, compression codec.
   - `test_rbac_delete_permissions`: Verifies `viewer` is rejected (HTTP 403) and `admin`/`superadmin` succeeds (HTTP 200/204).
   - `test_presigned_download_url_and_stream`: Tests presigned URL generation and fallback streaming.
2. **UI Integration Tests (`tests/test_ui_storage.py`)**:
   - `test_storage_page_renders_with_user_context`: Validates storage HTML template rendering and superadmin visibility.
   - `test_sql_editor_prefills_from_query_param`: Validates `/sql?q=...` prefilling in `ui/main.py`.
   - `test_ui_storage_proxy_endpoints`: Validates that UI correctly proxies `/api/v1/storage/...` calls to backend.
3. **End-to-End Regression**:
   - Run full pytest test suite across `tests/` to guarantee no regressions.

---

## 5. Implementation Task Checklist
- [ ] Add `pyarrow>=15.0.0` to `backend/requirements.txt`
- [ ] Implement `backend/storage_service.py`
- [ ] Implement `backend/storage_router.py`
- [ ] Register `storage_router` in `backend/main.py`
- [ ] Add storage proxy endpoints & `/sql?q=` pre-population in `ui/main.py`
- [ ] Rebuild `ui/templates/storage.html` with breadcrumbs, table explorer, Parquet modal, and DuckDB actions
- [ ] Add test suite `tests/test_backend_storage.py` and `tests/test_ui_storage.py`
- [ ] Run test suite and ruff formatting
- [ ] Close issue #109 and update GitHub Project board (`Lake of Tears (Main)`) to **Done**
