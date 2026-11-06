# Databricks notebook source
# MAGIC %md
# MAGIC # Grant Volume Access
# MAGIC
# MAGIC Admin utility: grants USE CATALOG + USE SCHEMA, READ VOLUME on
# MAGIC `read_volumes` and READ + WRITE VOLUME on `write_volumes` to a service
# MAGIC principal. It runs as a service principal that already has grant
# MAGIC authority on the target catalog.
# MAGIC
# MAGIC The app's own service principal is granted directly, because a bundle
# MAGIC `apps.*.resources` uc_securable block would make Terraform auto-grant
# MAGIC USE CATALOG to `account users`, which needs MANAGE on the catalog.
# MAGIC
# MAGIC If `run_as` also lacks GRANT rights on the target catalog, this job fails
# MAGIC with the same PERMISSION_DENIED the target service principal hits — a job
# MAGIC can't manufacture a privilege `run_as` doesn't already have.

# COMMAND ----------

"""Notebook entry point that grants a service principal access to Unity Catalog volumes."""
import logging

logger = logging.getLogger(__name__)

dbutils.widgets.text("service_principal", "", "Service principal client id (or account) to grant")
dbutils.widgets.text("catalog", "", "Catalog (e.g. uat_proj)")
dbutils.widgets.text("schema_name", "", "Schema (e.g. latlang)")
dbutils.widgets.text("read_volumes", "", "Comma-separated volumes granted READ VOLUME")
dbutils.widgets.text("write_volumes", "", "Comma-separated volumes granted READ + WRITE VOLUME")

service_principal = dbutils.widgets.get("service_principal").strip()
catalog           = dbutils.widgets.get("catalog").strip()
schema_name       = dbutils.widgets.get("schema_name").strip()
read_volumes      = [v.strip() for v in dbutils.widgets.get("read_volumes").split(",") if v.strip()]
write_volumes     = [v.strip() for v in dbutils.widgets.get("write_volumes").split(",") if v.strip()]

assert service_principal, "service_principal widget is required"
assert catalog, "catalog widget is required"
assert schema_name, "schema_name widget is required"
assert read_volumes or write_volumes, "read_volumes or write_volumes is required"

# COMMAND ----------

statements = [
    f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{service_principal}`",
    f"GRANT USE SCHEMA ON SCHEMA `{catalog}`.`{schema_name}` TO `{service_principal}`",
]
for volume in read_volumes + write_volumes:
    statements.append(f"GRANT READ VOLUME ON VOLUME `{catalog}`.`{schema_name}`.`{volume}` TO `{service_principal}`")
for volume in write_volumes:
    statements.append(f"GRANT WRITE VOLUME ON VOLUME `{catalog}`.`{schema_name}`.`{volume}` TO `{service_principal}`")

failed = []
for stmt in statements:
    try:
        spark.sql(stmt)
        logger.info("  OK: %s", stmt)
    except Exception as exc:
        failed.append((stmt, str(exc).splitlines()[0][:300]))
        logger.error("  FAILED: %s\n    -> %s", stmt, str(exc).splitlines()[0][:300])

if failed:
    raise RuntimeError(
        f"{len(failed)}/{len(statements)} grant statement(s) failed — run_as lacks "
        f"GRANT rights on {catalog}.{schema_name}; a real Unity Catalog "
        f"metastore/catalog admin needs to run this instead."
    )

logger.info("All grants applied successfully.")
