# Lake of Tears Community Connector Marketplace

Lake of Tears connectors allow automated, scheduled, or triggered ingestion of external data directly into workspace-scoped MinIO raw Parquet partitions.

Every connector is self-contained and defined using **LLM-optimized TOON (Token-Oriented Object Notation)** format.

---

## Contributing a New Connector

To contribute a new connector to the Lake of Tears ecosystem:

1. Create a directory: `pipeline/connectors/<connector_id>/`
2. Create `connector.toon`:

```toon
id: my_service
name: My Service
version: 1.0.0
category: SaaS & Finance       # Databases & Warehouses, Finance & SaaS, E-Commerce, CRM & Sales, Homelab & IoT, Media & Entertainment, Observability & Logs
author: Your GitHub Username
description: Brief description of what data is ingested into the lakehouse.
icon: cloud
dag_id: ingest_my_service
default_schedule: 0 2 * * *
docs_url: https://docs.example.com
parameters[2]{name,type,description}:
  api_key,string,Service API Key
  environment,string,Production or Sandbox
```

3. Create the ingestion script in `pipeline/ingest/my_service.py` using `StorageWriter`:

```python
import pandas as pd
from pipeline.shared.storage_writer import StorageWriter

def ingest_data(workspace_slug: str):
    # 1. Fetch data from source API
    records = fetch_my_service_records()
    df = pd.DataFrame(records)
    
    # 2. Write to workspace raw storage in Hive date partitions
    StorageWriter.write(df, source="my_service", workspace=workspace_slug)
```

4. Create the Airflow DAG in `pipeline/dags/ingest_my_service_dag.py` with `WORKSPACE_SLUG` parameterization.
5. Submit a Pull Request! The Lake of Tears platform will auto-discover your connector and make it available in the **Community Connector Hub**.
