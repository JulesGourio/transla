"""A sqlite stand-in for the asyncpg pool, enough to run the router's real SQL
for simple statements ($n placeholders, COALESCE/CASE, RETURNING, NOW()).
Postgres-only syntax (ANY($1::bigint[]), ON CONFLICT on expressions...) is out
of its reach — tests needing that stay at the function-logic level."""

import re
import sqlite3
from contextlib import asynccontextmanager

_SCHEMA = '''
CREATE TABLE translation_jobs (
    id INTEGER PRIMARY KEY, user_id TEXT, status TEXT DEFAULT 'uploaded', stage_progress TEXT,
    source_lang TEXT, target_lang TEXT, original_filename TEXT, input_volume_path TEXT,
    output_volume_path TEXT, segment_count INTEGER, needs_translation_count INTEGER,
    error_type TEXT, error_msg TEXT, worker_pid INTEGER, worker_heartbeat TEXT, updated_at TEXT,
    llm_call_count INTEGER DEFAULT 0, total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0, total_cost_eur REAL DEFAULT 0, notes TEXT,
    glossary_validated_at TEXT, share_token TEXT, selected_image_paths TEXT, page_filter TEXT
);
CREATE TABLE translation_segments (
    id INTEGER PRIMARY KEY, job_id INTEGER, seg_id TEXT, part TEXT, location_type TEXT,
    xml_choice_path TEXT, xml_fallback_path TEXT, source_text TEXT, detected_lang TEXT,
    lang_confidence REAL, pattern_type TEXT, pair_id TEXT, conflict_flag INTEGER DEFAULT 0,
    conflict_detail TEXT, dnt_tokens TEXT, inline_split TEXT, translated_text TEXT,
    keep_as_is INTEGER DEFAULT 0, warning_dismissed INTEGER DEFAULT 0, out_of_page_range INTEGER DEFAULT 0,
    UNIQUE (job_id, seg_id)
);
'''


def _sql(sql: str) -> str:
    return re.sub(r'\$(\d+)(::\w+(\[\])?)?', r'?\1', sql)


class FakeConn:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    async def execute(self, sql, *args):
        cur = self._db.execute(_sql(sql), args)
        return f'UPDATE {cur.rowcount}'

    async def fetch(self, sql, *args):
        return self._db.execute(_sql(sql), args).fetchall()

    async def fetchrow(self, sql, *args):
        return self._db.execute(_sql(sql), args).fetchone()

    async def fetchval(self, sql, *args):
        row = self._db.execute(_sql(sql), args).fetchone()
        return row[0] if row else None

    @asynccontextmanager
    async def transaction(self):
        yield


class FakePool:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.create_function('NOW', 0, lambda: '2026-10-05T00:00:00Z')
        self.db.executescript(_SCHEMA)

    @asynccontextmanager
    async def acquire(self, **_):
        yield FakeConn(self.db)

    def add_job(self, **fields):
        cols = ', '.join(fields)
        self.db.execute(f'INSERT INTO translation_jobs ({cols}) VALUES ({", ".join("?" * len(fields))})',
                        tuple(fields.values()))

    def add_segment(self, **fields):
        cols = ', '.join(fields)
        self.db.execute(f'INSERT INTO translation_segments ({cols}) VALUES ({", ".join("?" * len(fields))})',
                        tuple(fields.values()))

    def job(self, job_id=1):
        return dict(self.db.execute('SELECT * FROM translation_jobs WHERE id = ?', (job_id,)).fetchone())

    def segment(self, seg_id, job_id=1):
        return dict(self.db.execute('SELECT * FROM translation_segments WHERE job_id = ? AND seg_id = ?',
                                    (job_id, seg_id)).fetchone())
