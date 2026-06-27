"""Customer-local workflow: extract the EBS catalog -> save/load schema JSON ->
generate valid training data OVER that extracted schema. (Mock catalog, so it
runs with no Oracle -- the live path swaps MockCatalog -> OracleCatalog.)"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import example_from_schema  # noqa: E402
from tinyllm.extract import extract_schema  # noqa: E402
from tinyllm.schema_graph import SchemaGraph  # noqa: E402
from tinyllm.schema_graph.serialize import load_schema, save_schema  # noqa: E402
from tinyllm.train import make_local_split  # noqa: E402
from tinyllm.validate import validate_graph, validate_sqlglot  # noqa: E402


def test_extract_and_schema_json_roundtrip(tmp_path):
    schema = extract_schema(mock=True)
    assert "ap_invoices_all" in schema.table_names

    path = tmp_path / "schema.json"
    save_schema(schema, path)
    loaded = load_schema(path)

    assert loaded.table_names == schema.table_names
    fk = lambda s: {(f.from_table, f.from_column, f.to_table, f.to_column) for f in s.foreign_keys}
    assert fk(loaded) == fk(schema)
    # semantic annotations survive the round-trip
    seg2 = loaded.table("gl_code_combinations").column("segment2")
    assert seg2.business_label == "cost center"
    lk = loaded.table("ap_invoices_all").column("invoice_type_lookup_code")
    assert "STANDARD" in lk.allowed_values
    assert loaded.table("ap_invoices_all").is_multi_org is True


def test_data_generated_over_extracted_schema_is_valid():
    schema = extract_schema(mock=True)
    graph = SchemaGraph(schema)
    for i in range(40):
        ex = example_from_schema(schema, random.Random(i), level=1 + i % 5)
        assert ex.valid, ex.sql
        assert validate_graph(ex.ast, graph).ok


def test_make_local_split_over_extracted_schema():
    schema = extract_schema(mock=True)
    train, val = make_local_split(schema, n_train=40, n_val=10, paraphrases=1)
    assert train and val
    # every generated query is valid SQL and uses the customer's real tables
    for q, schema_str, sql in train[:25] + val:
        assert validate_sqlglot(sql).ok is not False
        assert any(t in sql for t in schema.table_names)
