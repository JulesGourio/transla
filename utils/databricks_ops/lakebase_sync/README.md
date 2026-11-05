# lakebase_sync

Moves data between Lakebase databases and Unity Catalog.

Parent: [databricks_ops](../README.md).

## Files

| File | Content |
|---|---|
| `copy_lakebase_to_uc.py` | Notebook. Copies every table of a Lakebase database to Delta tables of a Unity Catalog schema. The nightly job runs it |
| `migrate_translation_data_from_doccompare.py` | Notebook. One-time copy of the translation and glossary tables from another database of the same Lakebase project into this application's database |

## copy_lakebase_to_uc.py

Reads the Postgres catalog at every run, so nothing is hardcoded: tables and columns are discovered, and a column added to
Postgres reaches Delta on the next run.

- Rows are streamed with a server-side cursor and written with Spark. There is no intermediate file.
- All tables are read in one read-only `REPEATABLE READ` transaction, so the copy is one consistent snapshot.
- Delta column types come from the Postgres types: integers, decimals, booleans, dates, timestamps with and without time
  zone, arrays and binary. JSON, UUID, enums and anything else become `STRING`. A decimal wider than 38 digits becomes
  `STRING`, and a `numeric` without precision becomes `DOUBLE`.
- Each target table is created when missing and replaced at every run (`CREATE OR REPLACE`, atomic, history kept), empty
  tables included.
- A failing table does not stop the others; the run fails at the end if any table failed.
- A table removed from Postgres stays in Unity Catalog.
- It needs serverless compute, because classic clusters cannot reach the Lakebase private endpoint.

Parameters (widgets): `lakebase_project`, `lakebase_branch`, `lakebase_endpoint` (empty: the first), `lakebase_instance`
(for provisioned Lakebase), `lakebase_host`, `lakebase_database`, `source_schemas`, `include_tables`, `skip_tables`,
`target_catalog`, `target_schema`, `create_target_schema`, `target_table_name` (pattern with `{schema}` and `{table}`),
`fetch_batch_rows`. The defaults in this copy are the `latlang` database and the `uat_proj.latlang` schema.

The identity that runs it needs a Postgres role with `SELECT` on the source tables, and `USE CATALOG`, `USE SCHEMA` and
`CREATE TABLE` on the target. The two files in [grants](../grants) give those rights to the job's service principal.

The type mapping and the converters are pure functions in one cell of the notebook, and
[tests/test_lakebase_copy.py](../../../tests/test_lakebase_copy.py) runs that cell as it is.

## migrate_translation_data_from_doccompare.py

Copies `translation_jobs`, `translation_llm_calls`, `translation_segments`, `translation_questions`, `glossary_terms`,
`glossary_candidates` and `dnt_rules`, parents first, keeping the original ids. It only inserts: the application must have
created the empty tables in the target database (its first start does). The target is `latlang_test` by default, and the
notebook refuses `latlang` unless the parameter is changed on purpose.

## See also

- [resources](../../../resources/README.md): the job that schedules the copy.
- [docs/lakebase_schema.md](../../../docs/lakebase_schema.md): the tables copied.
