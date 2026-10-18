# DuckDB Querying and Lakehouse Analytics

Lake of Tears queries data **in place**: DuckDB reads the Parquet files in MinIO directly, so there is no separate query server and no loading step. This guide shows where you can run SQL, how DuckDB connects to MinIO, the query patterns that work well on a partitioned lake, how to run vector searches, and how to reach the data from notebooks and Apache Superset.

The queries in this guide were run against a local stack with three days of sample weather and storage data, and the results described are the real ones.

## 1. Where you can run SQL

| Where | Good for | Notes |
|---|---|---|
| **SQL Editor** in the Lake UI (`/sql`) | Interactive exploration; catalog tables appear as views | Runs in a pool of warm DuckDB connections with S3 already configured. See the write-access note below. |
| **AI Query, SQL assistant and Genie** | Plain-English questions, SQL generation, fixing and explaining queries | Read-only: only `SELECT`, `WITH`, `DESCRIBE`, `SUMMARIZE`, `SHOW`, `EXPLAIN` and `VALUES`, and a single statement |
| **Notebooks** (JupyterLab) | Analysis in Python with pandas | DuckDB is installed; MinIO credentials are in the environment |
| **Python scripts and DAGs** | Pipelines | `pipeline/shared/storage_writer.py` builds a connection |
| **Apache Superset** | BI dashboards | Needs a DuckDB driver that the image does not include; see section 6 |

> **Write access in the SQL Editor.** The SQL Editor currently runs whatever you submit. In our test, `COPY ... TO 's3://...'` and `CREATE TABLE` both succeeded there, while the AI endpoints refused them. Treat the editor as having write access to the whole bucket. Whether that should change is tracked in [#174](https://github.com/Lake-of-Tears/lake-of-tears/issues/174).

## 2. Connecting DuckDB to MinIO

DuckDB reads `s3://` paths through its `httpfs` extension. MinIO is S3-compatible, so you point DuckDB at the MinIO endpoint and use **path-style** URLs.

| Setting | Value for the Docker Compose stack |
|---|---|
| `s3_endpoint` | `minio:9000` from inside the stack, `localhost:9000` from your machine. Host and port only, with no `http://` |
| `s3_access_key_id` / `s3_secret_access_key` | `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` (the MinIO root user by default) |
| `s3_use_ssl` | `false` for the local stack; `true` if MinIO is behind TLS |
| `s3_url_style` | `path` (required for MinIO) |
| `s3_region` | `us-east-1` |

From Python (this is what the pipeline scripts and notebooks do):

```python
import os
import duckdb

con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")
con.execute(f"""
    SET s3_endpoint='{os.environ["MINIO_ENDPOINT"]}';
    SET s3_access_key_id='{os.environ["MINIO_ACCESS_KEY"]}';
    SET s3_secret_access_key='{os.environ["MINIO_SECRET_KEY"]}';
    SET s3_use_ssl=false;
    SET s3_url_style='path';
""")
```

The Lake UI builds its connections the same way and additionally turns on DuckDB's object cache (`enable_object_cache`), reuses a small pool of connections (`DUCKDB_POOL_SIZE`, default 4) and loads extensions "offline first": it tries `LOAD` and only falls back to `INSTALL` if the extension is missing.

If `s3_endpoint` has a scheme or a path (for example `http://minio:9000/`), the UI trims it to host and port; in your own code, pass only `host:port`.

## 3. Query patterns

All examples read the sample data written by the ingestion guide's layout: `s3://datalake/raw/weather/year=YYYY/month=MM/day=DD/data.parquet`.

### Read Parquet directly

```sql
SELECT *
FROM read_parquet('s3://datalake/raw/weather/**/*.parquet')
LIMIT 10;
```

`**` matches every partition folder. Always add `LIMIT` while exploring, because there is no row cap on queries from the SQL Editor.

### Hive partition columns come for free

The `year=`, `month=` and `day=` folder names become **columns**:

```sql
SELECT year, month, day, count(*) AS rows
FROM read_parquet('s3://datalake/raw/weather/**/*.parquet')
GROUP BY ALL
ORDER BY ALL;
```

In `DESCRIBE`, the partition columns are listed after the file's own columns.

**Watch the column types.** The pipeline writes zero-padded folders such as `month=09` and `day=05`. DuckDB guesses each partition column's type from the folder values, and a zero-padded value is not a clean integer, so as soon as your lake has a single-digit month or day, `month` and `day` become **`VARCHAR`** (`year` stays `BIGINT`). We tested with `month=09` folders:

- Filters still work, because DuckDB converts the number: `WHERE day = 5` matched the `day=05` folder.
- Functions that need numbers fail: `make_date(year, month, day)` gave `No function matches the given name and argument types 'make_date(BIGINT, VARCHAR, VARCHAR)'`.

Fix it by telling DuckDB the types with `hive_types`, which the examples below do:

```sql
SELECT make_date(year, month, day) AS d, count(*) AS rows
FROM read_parquet(
  's3://datalake/raw/weather/**/*.parquet',
  hive_types = {'year': INTEGER, 'month': INTEGER, 'day': INTEGER}
)
GROUP BY ALL
ORDER BY d;
```

(Casting works too: `make_date(year::INTEGER, month::INTEGER, day::INTEGER)`.)

### Filter by partition to read less

Filtering on `year`, `month` and `day` lets DuckDB skip whole files, whichever type the columns have. We checked with three daily files:

```sql
EXPLAIN
SELECT count(*)
FROM read_parquet('s3://datalake/raw/weather/**/*.parquet')
WHERE year = 2026 AND month = 10 AND day = 15;
```

The plan shows **`Scanning Files: 1/3`**, and counting `filename` (add `filename = true` to `read_parquet`) returns 1 file with the filter and 3 without. On a lake with years of daily files, always include the date columns in your `WHERE` clause.

### Combine sources

Build a real date from the partition columns to join sources by day, for example weather against storage use (with `hive_types`, as explained above):

```sql
WITH w AS (
  SELECT make_date(year, month, day) AS d, avg(temperature_2m) AS avg_temp
  FROM read_parquet('s3://datalake/raw/weather/**/*.parquet',
                    hive_types = {'year': INTEGER, 'month': INTEGER, 'day': INTEGER})
  GROUP BY ALL
),
n AS (
  SELECT make_date(year, month, day) AS d, max(allocated) / 1024.0^3 AS used_gb
  FROM read_parquet('s3://datalake/raw/truenas/**/*.parquet',
                    hive_types = {'year': INTEGER, 'month': INTEGER, 'day': INTEGER})
  GROUP BY ALL
)
SELECT w.d, round(w.avg_temp, 1) AS avg_temp, round(n.used_gb) AS used_gb
FROM w JOIN n USING (d)
ORDER BY w.d;
```

Joining directly on the `source_date` column that ingestion adds works too (`... JOIN ... USING (source_date)`).

### Trends with window functions

Aggregate first, then apply the window function in an outer query. Mixing a window and a `GROUP BY` in one `SELECT` fails with `Cannot mix aggregates with non-aggregated columns`:

```sql
WITH d AS (
  SELECT make_date(year, month, day) AS d, avg(temperature_2m) AS t
  FROM read_parquet('s3://datalake/raw/weather/**/*.parquet',
                    hive_types = {'year': INTEGER, 'month': INTEGER, 'day': INTEGER})
  GROUP BY ALL
)
SELECT d, round(t, 1) AS t,
       round(avg(t) OVER (ORDER BY d ROWS 1 PRECEDING), 1) AS moving_avg
FROM d
ORDER BY d;
```

### Inspect a dataset

```sql
DESCRIBE SELECT * FROM read_parquet('s3://datalake/raw/weather/**/*.parquet');
SUMMARIZE SELECT temperature_2m FROM read_parquet('s3://datalake/raw/weather/**/*.parquet');
```

`SUMMARIZE` returns min, max, average, quartiles and null percentage per column.

### When columns change over time

By default DuckDB takes the column list from the first file and **ignores columns that appear only in later files**. After a day with a new `uv_index` column, `SELECT uv_index ...` failed with `Referenced column "uv_index" not found`. Add `union_by_name = true` and the column is available, with NULLs for earlier days:

```sql
SELECT day, uv_index
FROM read_parquet('s3://datalake/raw/weather/**/*.parquet', union_by_name = true);
```

### Catalog tables

Tables registered in your workspace's catalogs are exposed in the SQL Editor as views named **`schema.table`**:

```sql
SELECT count(*) FROM bronze.weather;
```

Know the limits:

- The **catalog name is not part of the view name**. Using `production.bronze.weather` fails with `Catalog "production" does not exist`.
- If two catalogs in a workspace have the same schema and table names, they **collide silently**: both views were registered under `bronze.weather` and the query read only the last one. Keep schema and table names unique across the catalogs a workspace can see.

See [Workspaces and Catalog Management](Workspaces-and-Catalog-Management.md) for how catalogs and tables are defined.

### SQL AI functions

The SQL Editor and Genie register four AI functions that call Gemini row by row: `ai_query`, `ai_summarize`, `ai_classify` and `ai_extract`. By default they process at most **50 rows per query** (`AI_MAX_ROWS_PER_QUERY`), so keep a `LIMIT` and narrow your filter first, for example:

```sql
SELECT source, ai_summarize(content) AS brief
FROM read_parquet('s3://datalake/embeddings/**/*.parquet')
LIMIT 5;
```

They need a Gemini key configured in the Admin Panel. We did not run them in these tests, because the test stack has no key.

## 4. Vector search SQL

Embeddings are stored as lists of doubles (`DOUBLE[]`) in `embeddings/<source>/...`. Cosine similarity needs fixed-size arrays, so cast both sides to `DOUBLE[768]`:

```sql
SELECT content,
       array_cosine_similarity(embedding::DOUBLE[768], $query_vec::DOUBLE[768]) AS score
FROM read_parquet('s3://datalake/embeddings/**/*.parquet')
ORDER BY score DESC
LIMIT 5;
```

Verified details:

- The query vector can be passed three ways, all giving the same score: a positional `?`, a named `$query_vec` (Python: `con.execute(sql, {"query_vec": vec})`), or `list_cosine_similarity(embedding, ?)`, which works on lists without a cast.
- **The `vss` extension is not needed** for these functions. The UI and pipeline load it so that an index can be added later (`INSTALL vss; LOAD vss;`), but today every search scans the matching files.
- The vector must have **768** numbers, from the same model as the stored vectors.

How the vectors are created, and how to embed a question, is covered in [Chunking and Vector Embeddings](Chunking-and-Vector-Embeddings.md).

## 5. Notebooks and visualization

### JupyterLab notebooks

The Jupyter container has DuckDB installed and the MinIO settings as environment variables, so a notebook cell can connect without copying credentials:

```python
import os
import duckdb

con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")
con.execute(f"""
    SET s3_endpoint='{os.environ["MINIO_ENDPOINT"]}';
    SET s3_access_key_id='{os.environ["MINIO_ACCESS_KEY"]}';
    SET s3_secret_access_key='{os.environ["MINIO_SECRET_KEY"]}';
    SET s3_use_ssl=false;
    SET s3_url_style='path';
""")

df = con.execute("""
    SELECT year, month, day, count(*) AS n, round(avg(temperature_2m), 1) AS avg_temp
    FROM read_parquet('s3://datalake/raw/weather/**/*.parquet', union_by_name = true)
    GROUP BY ALL ORDER BY ALL
""").df()
df
```

`.df()` returns a pandas DataFrame, ready for plotting. We ran this in the Jupyter container and it returned one row per day.

## 6. Apache Superset with DuckDB

Superset runs at `/superset/`, but the Superset image **does not include a DuckDB driver**, and no DuckDB database is pre-configured. To use it you must extend the image, and then add a database in Superset.

1. Add the SQLAlchemy driver and pre-install the S3 extension in the Superset image (the extension has to be installed ahead of time, because Superset only asks DuckDB to `LOAD` it):

   ```dockerfile
   FROM apache/superset:4.1.4
   USER root
   RUN pip install duckdb-engine \
    && python -c "import duckdb; duckdb.execute('INSTALL httpfs')"
   USER superset
   ```

2. In Superset choose **Settings, Database Connections, + Database, Other** and use the SQLAlchemy URI `duckdb:///:memory:`.
3. Under **Advanced, Other, Engine Parameters** add (adjust the endpoint and keys):

   ```json
   {
     "connect_args": {
       "preload_extensions": ["httpfs"],
       "config": {
         "s3_endpoint": "minio:9000",
         "s3_access_key_id": "<access key>",
         "s3_secret_access_key": "<secret key>",
         "s3_use_ssl": false,
         "s3_url_style": "path"
       }
     }
   }
   ```

4. Create a dataset from a SQL query that uses `read_parquet('s3://datalake/...')` and build charts from it.

We checked steps 1 and 3 with the Superset 4.1.4 image on the Compose network: after installing `duckdb-engine` and `httpfs`, a SQLAlchemy engine with those `connect_args` returned the per-day row counts from MinIO. We did not click through the Superset UI. Two cautions: engine parameters, including the keys, are stored in Superset's own database, so use a MinIO user limited to read access; and `ModuleNotFoundError: No module named 'duckdb_engine'` or `Extension "httpfs" ... not found` means step 1 was skipped.

## 7. Performance tips

- **Filter by `year`, `month` and `day`** (or `source_date`) first. Partition pruning is the biggest win.
- **Select only the columns you need.** Parquet is columnar, so unused columns are never read.
- **Add `LIMIT` while exploring**, and aggregate in SQL instead of pulling rows into Python.
- Keep partition files reasonably sized; many tiny files are slow to list and open (compaction is a planned improvement).
- Vector search scans every embedding file until an index exists ([#107](https://github.com/Lake-of-Tears/lake-of-tears/issues/107)), so narrow with `WHERE source = ...` or a date range first.

## 8. Troubleshooting

The SQL Editor classifies failures and shows a hint:

| Error shown | Typical cause | What to do |
|---|---|---|
| **Empty Dataset (No Parquet Files Found)** | The glob matches nothing | Check the path and that ingestion has written data |
| **S3 / MinIO Connection or Authentication Error** | Wrong endpoint or keys, MinIO down | Check `s3_endpoint`, the keys and `s3_url_style='path'` |
| **SQL Syntax or Schema Error** | A column or table that does not exist | Check `DESCRIBE`; for new columns use `union_by_name = true`; use **Fix with AI** |
| **Query Blocked by Read-Only Policy** | A write statement sent through an AI endpoint | Use `SELECT`, `WITH`, `DESCRIBE`, `SUMMARIZE` or `SHOW` |
| **Gemini AI Function Execution Error** | An `ai_*` function failed | Check the Gemini key in the Admin Panel and quota |

Other messages seen in testing:

| Message | Meaning |
|---|---|
| `Catalog "<name>" does not exist` | Catalog tables are `schema.table`; do not prefix the catalog name |
| `Cannot mix aggregates with non-aggregated columns` | Aggregate in a CTE, then apply the window function |
| `No function matches ... make_date(BIGINT, VARCHAR, VARCHAR)` | Zero-padded folders made `month` and `day` text; add `hive_types = {'year': INTEGER, 'month': INTEGER, 'day': INTEGER}` |
| `Cannot cast list with length N to array with length 768` | The vector has the wrong length |
| `Extension "httpfs" ... not found` | Run `INSTALL httpfs` first |

## Related pages

- [Home](Home.md)
- [Architecture Overview](Architecture-Overview.md): the storage layout these queries read
- [Data Ingestion and Pipelines](Data-Ingestion-and-Pipelines.md): how partitions are written
- [Chunking and Vector Embeddings](Chunking-and-Vector-Embeddings.md): the vectors behind section 4
- [Workspaces and Catalog Management](Workspaces-and-Catalog-Management.md): catalog tables
