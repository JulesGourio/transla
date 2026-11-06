# databricks_ops

Notebooks that run as Databricks jobs, and the files they use.

Parent: [utils](../README.md). The jobs are declared in [databricks.yml](../../databricks.yml) and
[resources](../../resources/README.md).

## Files

| Path | Content |
|---|---|
| `app_lifecycle_job.py` | Starts or stops the app (widgets `app_name`, `action`). Backs the weekday jobs that keep the app running from 08:00 to 19:00 Paris time |
| [`lakebase_sync/copy_lakebase_to_uc.py`](lakebase_sync/README.md) | Copies every table of the Lakebase database to Delta tables |
| [`lakebase_sync/migrate_translation_data_from_doccompare.py`](lakebase_sync/README.md) | One-time copy of the translation tables from another database of the same Lakebase project |
| `grants/lakebase_copy_schema.json` | Grant to apply to the schema so the copy job can write: `USE_SCHEMA` and `CREATE_TABLE` for the job's service principal |
| `grants/lakebase_copy_pg_role.json` | Postgres role to create for that service principal: `SERVICE_PRINCIPAL`, `LAKEBASE_OAUTH_V1`, member of `DATABRICKS_SUPERUSER` |

## app_lifecycle_job.py

Reads the state of the app first and does nothing when it is already in the requested state, because the SDK raises on a
start of a running app or a stop of a stopped one.

## Rules

- A notebook declares its parameters as widgets.

## See also

- [lakebase_sync](lakebase_sync/README.md): the Lakebase copy.
- [HANDOVER.md](../../HANDOVER.md): when each job is run.
