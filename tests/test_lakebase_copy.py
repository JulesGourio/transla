"""Pure functions of the Lakebase -> Unity Catalog copy notebook, extracted
from its "Pure functions" cell so the notebook stays the single source."""

import decimal
import re
from pathlib import Path

import pytest

NOTEBOOK = Path(__file__).parent.parent / 'utils/databricks_ops/lakebase_sync/copy_lakebase_to_uc.py'


def _load_pure_cell() -> dict:
    cells = NOTEBOOK.read_text(encoding='utf-8').split('# COMMAND ----------')
    cell = next(c for c in cells if 'DBTITLE 1,Pure functions' in c)
    namespace: dict = {}
    exec(cell, namespace)
    return namespace


NS = _load_pure_cell()


@pytest.mark.parametrize('pg_type,expected', [
    ('integer', 'INT'),
    ('bigint', 'BIGINT'),
    ('text', 'STRING'),
    ('jsonb', 'STRING'),
    ('boolean', 'BOOLEAN'),
    ('timestamp with time zone', 'TIMESTAMP'),
    ('timestamp(3) without time zone', 'TIMESTAMP_NTZ'),
    ('numeric(10,4)', 'DECIMAL(10,4)'),
    ('numeric', 'DOUBLE'),
    ('numeric(50,2)', 'STRING'),
    ('text[]', 'ARRAY<STRING>'),
    ('integer[]', 'ARRAY<INT>'),
])
def test_map_pg_type(pg_type, expected):
    assert NS['map_pg_type'](pg_type) == expected


def test_converters():
    assert NS['make_converter']('STRING')({'a': 1}) == '{"a": 1}'
    assert NS['make_converter']('DECIMAL(10,4)')('1.5') == decimal.Decimal('1.5')
    assert NS['make_converter']('ARRAY<INT>')(['1', '2']) == [1, 2]
    assert NS['make_converter']('INT')(None) is None


def test_target_names_and_collisions():
    names = NS['resolve_target_names']([('public', 'glossary_terms')], '{table}')
    assert names == {('public', 'glossary_terms'): 'glossary_terms'}
    with pytest.raises(ValueError, match='collision'):
        NS['resolve_target_names']([('a', 't'), ('b', 'T')], '{table}')
    with pytest.raises(ValueError):
        NS['resolve_target_names']([('a', 't')], 'fixed')


def test_latlang_tables_all_map():
    # Every column type the app's own schema uses must map to a concrete Spark type.
    ddl = (Path(__file__).parent.parent / 'server/services/lakebase.py').read_text(encoding='utf-8')
    types = set(re.findall(r'^\s+\w+\s+(SERIAL|INTEGER|BIGINT|TEXT|BOOLEAN|JSONB|TIMESTAMPTZ|DOUBLE PRECISION|NUMERIC\(\d+,\s*\d+\)|REAL|DATE)\b', ddl, re.M))
    pg_names = {'SERIAL': 'integer', 'TIMESTAMPTZ': 'timestamp with time zone'}
    for t in types:
        assert NS['map_pg_type'](pg_names.get(t, t.lower()))
