# resources

Databricks jobs declared as part of the bundle, one file per concern. [databricks.yml](../databricks.yml) includes `resources/*.yml`.

Parent: [root README](../README.md).

## Files

| File | Content |
|---|---|
| `grants.yml` | The manual job `latlang-grant-volume-access-<target>`, declared for every target |
| `app_schedules.yml` | The jobs `WD_0_latlang_start_app_latlang-uat` and `WD_1_latlang_stop_app_latlang-uat`: start at 08:00 and stop at 19:00, Monday to Friday, Paris time (UAT only) |
| `lakebase_copy.yml` | The job `D_0_latlang-lakebase-copy-latlang-uat`: copies the `latlang` Lakebase database to Delta tables every night at 02:00 (Europe/Paris) |

## Job format

Every job sets `queue`, an `on_failure` e-mail to `${var.notification_email}`, `no_alert_for_canceled_runs`, and the tags
`activity-type` (`${var.job_activity_tag}`, `<env>-job`), `job-purpose` and `project-name`. The tests check it.

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

## grants.yml and app_schedules.yml

The grant job gives the app's service principal `USE CATALOG`, `USE SCHEMA`, `READ VOLUME` on `binaries` and `READ VOLUME` +
`WRITE VOLUME` on `documents`. Without `run_as` (DEV) it runs as the user who starts it, who needs `MANAGE` on the catalog;
in UAT it runs as the UAT job service principal. The start and stop jobs run as that service principal, which holds
`CAN_MANAGE` on the app.

## See also

- [databricks_ops](../utils/databricks_ops/README.md): the notebooks these jobs run.
- [utils/deploy](../utils/deploy/README.md): `-Infra` deploys this bundle.
