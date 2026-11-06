# Databricks notebook source
# DBTITLE 1,One-time data migration — doccompare -> latlang(_test)
"""Copies historical Translate/glossary tables out of an existing sibling
app's Lakebase database (doccompare) into this app's own database
(latlang_test by default, never latlang directly).

Both databases live in the SAME Lakebase project, so this is two plain
psycopg2 connections to the same host with a different `database=`, not a
cross-project operation.

Run this ONCE after this app's own _ensure_schema() has created the empty
tables in the target database (i.e. after the app's first successful
startup) — this script only INSERTs, it doesn't create tables.

Table order matters: parents (translation_jobs, glossary_terms) before
children with FK references (translation_llm_calls/segments/questions
reference translation_jobs.id — copy jobs first so IDs match, since we
preserve the source SERIAL ids explicitly).
"""
import datetime
import json
import logging
from decimal import Decimal

import psycopg2
import psycopg2.extras
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

dbutils.widgets.text("LAKEBASE_PROJECT_ID", "qualibot")
dbutils.widgets.text("LAKEBASE_BRANCH", "production")
dbutils.widgets.text("LAKEBASE_ENDPOINT", "primary")
dbutils.widgets.text("SOURCE_LAKEBASE_DATABASE", "doccompare")
# Defaults to the test database so the real latlang is never overwritten by accident.
dbutils.widgets.text("TARGET_LAKEBASE_DATABASE", "latlang_test")

LAKEBASE_PROJECT_ID = dbutils.widgets.get("LAKEBASE_PROJECT_ID")
LAKEBASE_BRANCH = dbutils.widgets.get("LAKEBASE_BRANCH")
LAKEBASE_ENDPOINT = dbutils.widgets.get("LAKEBASE_ENDPOINT")
SOURCE_LAKEBASE_DATABASE = dbutils.widgets.get("SOURCE_LAKEBASE_DATABASE")
TARGET_LAKEBASE_DATABASE = dbutils.widgets.get("TARGET_LAKEBASE_DATABASE")

if TARGET_LAKEBASE_DATABASE == "latlang":
    raise RuntimeError(
        "TARGET_LAKEBASE_DATABASE='latlang' refusé sur ce job tant que la "
        "migration n'a pas été validée sur latlang_test. Passez ce "
        "paramètre explicitement une fois la validation faite."
    )

# Parents first, then children referencing them via job_id/term FK.
TABLES_IN_ORDER = [
    "translation_jobs",
    "translation_llm_calls",
    "translation_segments",
    "translation_questions",
    "glossary_terms",
    "glossary_candidates",
    "dnt_rules",
]


def json_default(obj: object) -> object:
    """Serialize values that ``json.dumps`` cannot handle natively.

    :param obj: Value rejected by the JSON encoder.
    :return: ISO string for dates and datetimes, float for ``Decimal``, ``str(obj)`` otherwise.
    """
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


w = WorkspaceClient()
branch_path = f"projects/{LAKEBASE_PROJECT_ID}/branches/{LAKEBASE_BRANCH}"
endpoint_path = f"{branch_path}/endpoints/{LAKEBASE_ENDPOINT}"
endpoints = list(w.postgres.list_endpoints(parent=branch_path))
if not endpoints:
    raise RuntimeError(f"Aucun endpoint trouvé pour {branch_path}")
endpoint = next((ep for ep in endpoints if getattr(ep, "name", None) == LAKEBASE_ENDPOINT), endpoints[0])
lakebase_host = endpoint.status.hosts.host

me = w.current_user.me()
username = me.user_name or me.display_name
if not username:
    raise RuntimeError("Impossible de résoudre l'identité courante.")

credential = w.postgres.generate_database_credential(endpoint=endpoint_path)
if not credential.token:
    raise RuntimeError("generate_database_credential a retourné un token vide.")


def connect(database: str) -> "psycopg2.extensions.connection":
    """Open a TLS connection to the shared Lakebase host.

    :param database: Name of the Postgres database to connect to.
    :return: Open psycopg2 connection authenticated with the current user's OAuth token.
    """
    return psycopg2.connect(
        host=lakebase_host, port=5432, database=database,
        user=username, password=credential.token, sslmode="require",
    )


src = connect(SOURCE_LAKEBASE_DATABASE)
dst = connect(TARGET_LAKEBASE_DATABASE)

try:
    for table in TABLES_IN_ORDER:
        with src.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f'SELECT * FROM "{table}"')
            rows = [dict(r) for r in cur.fetchall()]

        if not rows:
            logger.info("  %s: 0 ligne — rien à migrer", table)
            continue

        columns = list(rows[0].keys())
        col_list = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        insert_sql = (
            f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) '
            f'ON CONFLICT (id) DO NOTHING'
        )
        values = [
            tuple(
                json.dumps(v, default=json_default) if isinstance(v, (dict, list)) else v
                for v in row.values()
            )
            for row in rows
        ]
        with dst.cursor() as cur:
            psycopg2.extras.execute_batch(cur, insert_sql, values)
        dst.commit()

        # Sequences must follow the copied ids or the next DEFAULT insert collides.
        with dst.cursor() as cur:
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM \"{table}\"), 1))"
            )
        dst.commit()

        logger.info("  %s: %d ligne(s) migrée(s)", table, len(rows))

    logger.info("Terminé — %s -> %s", SOURCE_LAKEBASE_DATABASE, TARGET_LAKEBASE_DATABASE)
finally:
    src.close()
    dst.close()


# COMMAND ----------
