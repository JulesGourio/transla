# databricks_ops

Notebooks that run as Databricks jobs, and the files they use.

Parent: [utils](../README.md). The jobs are declared in [databricks.yml](../../databricks.yml) and
[resources](../../resources/README.md).

## Files

| Path | Content |
|---|---|
| `app_lifecycle_job.py` | Starts or stops the app (widgets `app_name`, `action`). Backs the weekday jobs that keep the app running from 08:00 to 19:00 Paris time |
| `grant_volume_access_job.py` | Grants a service principal `USE CATALOG`, `USE SCHEMA`, `READ VOLUME` on the read volumes and `READ VOLUME` plus `WRITE VOLUME` on the write volumes. Manual job |
| [`lakebase_sync/copy_lakebase_to_uc.py`](lakebase_sync/README.md) | Copies every table of the Lakebase database to Delta tables |
| [`lakebase_sync/migrate_translation_data_from_doccompare.py`](lakebase_sync/README.md) | One-time copy of the translation tables from another database of the same Lakebase project |
| `grants/lakebase_copy_schema.json` | Grant to apply to the schema so the copy job can write: `USE_SCHEMA` and `CREATE_TABLE` for the job's service principal |
| `grants/lakebase_copy_pg_role.json` | Postgres role to create for that service principal: `SERVICE_PRINCIPAL`, `LAKEBASE_OAUTH_V1`, member of `DATABRICKS_SUPERUSER` |

## app_lifecycle_job.py

Reads the state of the app first and does nothing when it is already in the requested state, because the SDK raises on a
start of a running app or a stop of a stopped one.

## grant_volume_access_job.py

Parameters: `service_principal` (the app's service principal client id), `catalog`, `schema_name`, `read_volumes`,
`write_volumes` (comma-separated). It runs as a service principal that already holds grant rights on the catalog; a run as
anyone else fails with `PERMISSION_DENIED`, and the job says so. The app's grants are given here rather than through a
`resources` block in the bundle, because the bundle would then try to grant `USE CATALOG` to every account user and fail
unless the deployer manages the catalog.

The id to pass is the `service_principal_client_id` of `databricks apps get latlang`.

## Rules

- A notebook declares its parameters as widgets.
- A grant job applies every statement and reports each failure at the end, so one refused grant hides nothing.

## See also

- [lakebase_sync](lakebase_sync/README.md): the Lakebase copy.
- [HANDOVER.md](../../HANDOVER.md): when each job is run.
