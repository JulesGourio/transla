"""Shared Lakebase glossary helpers (term id allocation).

glossary_io.py is the separate CSV-file glossary.
"""

from typing import Any

TERM_ID_LOCK_KEY = "glossary_terms.term_id"


async def generate_term_id(conn: Any) -> str:
    """Atomically allocate the next "T####" glossary_terms id.

    Must run inside an open conn.transaction(): the transaction-scoped advisory lock it takes serializes concurrent callers, and would release before the caller's INSERT outside a transaction.

    :param conn: asyncpg connection inside a transaction.
    :return: the new term id.
    """
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", TERM_ID_LOCK_KEY)
    max_id = await conn.fetchval(
        r"SELECT COALESCE(MAX(SUBSTRING(term_id FROM 2)::int), 0) FROM glossary_terms WHERE term_id ~ '^T\d+$'"
    )
    return f'T{(max_id or 0) + 1:04d}'
