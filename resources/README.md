# resources

Databricks jobs declared as part of the bundle. [databricks.yml](../databricks.yml) includes `resources/*.yml`.

Parent: [root README](../README.md).

## Files

| File | Content |
|---|---|
| `lakebase_copy.yml` | The job `D_0_latlang-lakebase-copy-latlang-uat`: copies the `latlang` Lakebase database to Delta tables every night at 02:00 (Europe/Paris) |

## lakebase_copy.yml

Runs [copy_lakebase_to_uc.py](../utils/databricks_ops/lakebase_sync/README.md) on serverless compute (environment version
5, `psycopg2-binary`) as the UAT job service principal, one run at a time, with a one-hour timeout. Target: the schema
`<app_write_catalog>.<app_write_schema>`. Permissions: the CoreAdmin and CoreDev groups manage it.

Requirements, all outside the job file:

- the service principal has a Postgres role on the Lakebase project (see the JSON in
  [grants](../utils/databricks_ops/grants/lakebase_copy_pg_role.json));
- it holds `USE_SCHEMA` and `CREATE_TABLE` on the schema ([grants](../utils/databricks_ops/grants/lakebase_copy_schema.json)).

The Delta tables carry the names of the Postgres tables (`translation_jobs`, `translation_segments`, `glossary_terms`,
...). They are for analysis and backup; the application never reads them.

## Other jobs of the bundle

`databricks.yml`, in the `latlang-uat` target, declares three more jobs:

| Job | Does |
|---|---|
| `latlang-grant-volume-access-latlang-uat` | Manual. Grants the app's service principal its rights on the volumes |
| `WD_0_latlang_start_app_latlang-uat` | Starts the app at 08:00, Monday to Friday, Paris time |
| `WD_1_latlang_stop_app_latlang-uat` | Stops it at 19:00 on the same days |

The jobs run as the UAT job service principal, which holds `CAN_MANAGE` on the app so it can start and stop it.

## See also

- [databricks_ops](../utils/databricks_ops/README.md): the notebooks these jobs run.
- [utils/deploy](../utils/deploy/README.md): `-Infra` deploys this bundle.
