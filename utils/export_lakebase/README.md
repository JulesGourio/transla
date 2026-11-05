# export_lakebase

`create_proj_latlang_tables.py` is the same Lakebase to Unity Catalog copy notebook as
[`copy_lakebase_to_uc.py`](../databricks_ops/lakebase_sync/README.md), with neutral defaults: no project, the Postgres
database `databricks_postgres`, no target catalog or schema, and `create_target_schema` on. Use it to copy a Lakebase
database that is not the application's. No job runs it.

Parent: [utils](../README.md).

## See also

- [lakebase_sync](../databricks_ops/lakebase_sync/README.md): the copy notebook the job runs, and its parameters.
