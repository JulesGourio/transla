"""Shared helper: run SQL statements against the UAT warehouse via the Databricks CLI (statements API)."""

import json
import subprocess
import time

DEFAULT_WAREHOUSE = '5890912c31867b77'  # Serverless Starter Warehouse (UAT)
DEFAULT_PROFILE = 'UAT'
CHUNKS_TABLE = 'uat_landingzone.qualibot.chunks_v2'


def _cli_json(args: list[str]) -> dict:
    """Run a Databricks CLI command and parse its JSON output.

    :param args: Full command line, executable first.
    :return: Parsed JSON object printed by the command.
    """
    # Explicit encoding: the Windows default codec cannot decode the API's UTF-8 responses.
    return json.loads(subprocess.check_output(args, text=True, encoding='utf-8'))


def run_sql(statement: str, warehouse: str = DEFAULT_WAREHOUSE,
            profile: str = DEFAULT_PROFILE) -> list[list]:
    """Run one SQL statement and collect every result row.

    :param statement: SQL text to execute.
    :param warehouse: Id of the SQL warehouse that runs the statement.
    :param profile: Databricks CLI profile used to authenticate.
    :return: Result rows as lists of string values (all result chunks concatenated).
    :raises RuntimeError: if the statement does not end in the SUCCEEDED state.
    """
    payload = json.dumps({'warehouse_id': warehouse, 'statement': statement, 'wait_timeout': '50s'})
    d = _cli_json(['databricks', 'api', 'post', '/api/2.0/sql/statements', '-p', profile, '--json', payload])
    while d.get('status', {}).get('state') in ('PENDING', 'RUNNING'):
        time.sleep(3)
        d = _cli_json(['databricks', 'api', 'get', f"/api/2.0/sql/statements/{d['statement_id']}", '-p', profile])
    state = d.get('status', {}).get('state')
    if state != 'SUCCEEDED':
        raise RuntimeError(f'SQL failed ({state}): {d.get("status")}')
    rows = d.get('result', {}).get('data_array', []) or []
    # Large results are split into chunks.
    next_idx = d.get('result', {}).get('next_chunk_index')
    while next_idx is not None:
        c = _cli_json(['databricks', 'api', 'get',
                       f"/api/2.0/sql/statements/{d['statement_id']}/result/chunks/{next_idx}", '-p', profile])
        rows.extend(c.get('data_array', []) or [])
        next_idx = c.get('next_chunk_index')
    return rows


# REF language-suffix conventions of the corpus (GB/UK/US map to en); Czech documents use CZ, CS is accepted too.
LANG_SUFFIX_TO_CODE = {
    'FR': 'fr', 'EN': 'en', 'GB': 'en', 'UK': 'en', 'US': 'en',
    'BG': 'bg', 'ES': 'es', 'CS': 'cs', 'CZ': 'cs', 'DE': 'de',
}
SUFFIX_RE = r'[_-](FR|GB|EN|DE|ES|CS|CZ|BG|US|UK)$'
