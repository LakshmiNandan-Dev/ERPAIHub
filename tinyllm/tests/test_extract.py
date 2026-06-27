"""EBS catalog extraction: raw data-dictionary rows -> a semantic, join-aware
Schema. The hard part is inferring FKs (EBS declares none) and flexfield/lookup
meaning, and producing a Schema the rest of the pipeline consumes unchanged."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm.extract import EbsExtractor, MockCatalog  # noqa: E402
from tinyllm.schema_graph import SchemaGraph  # noqa: E402
from tinyllm.schema_graph.types import SemanticRole  # noqa: E402
from tinyllm.sql_sampler import QuerySampler  # noqa: E402
from tinyllm.validate import validate_graph  # noqa: E402


def _schema():
    return EbsExtractor(MockCatalog()).extract()


def test_tables_and_multi_org():
    s = _schema()
    assert set(s.table_names) == {
        "ap_suppliers", "ap_invoices_all", "ap_invoice_lines_all", "gl_code_combinations"
    }
    assert s.table("ap_invoices_all").is_multi_org is True       # _ALL + org_id
    assert s.table("ap_invoice_lines_all").is_multi_org is True
    assert s.table("ap_suppliers").is_multi_org is False
    assert s.table("gl_code_combinations").is_multi_org is False


def test_foreign_keys_inferred_from_conventions():
    s = _schema()
    edges = {(fk.from_table, fk.from_column, fk.to_table, fk.to_column)
             for fk in s.foreign_keys}
    # MockCatalog declares ZERO FKs; all of these are inferred by naming convention
    assert ("ap_invoices_all", "vendor_id", "ap_suppliers", "vendor_id") in edges
    assert ("ap_invoice_lines_all", "invoice_id", "ap_invoices_all", "invoice_id") in edges
    assert ("ap_invoice_lines_all", "code_combination_id",
            "gl_code_combinations", "code_combination_id") in edges
    # org_id is striping, NOT a foreign key
    assert not any(fk.from_column == "org_id" for fk in s.foreign_keys)


def test_semantic_roles():
    s = _schema()
    inv = s.table("ap_invoices_all")
    assert inv.column("invoice_id").role == SemanticRole.ID and inv.column("invoice_id").is_pk
    assert inv.column("invoice_amount").role == SemanticRole.AMOUNT
    assert inv.column("invoice_date").role == SemanticRole.DATE
    assert inv.column("org_id").role == SemanticRole.ORG_ID
    assert s.table("ap_suppliers").column("vendor_name").role == SemanticRole.NAME


def test_flexfield_and_lookup_metadata():
    s = _schema()
    seg2 = s.table("gl_code_combinations").column("segment2")
    assert seg2.role == SemanticRole.FLEXFIELD_SEGMENT
    assert seg2.business_label == "cost center"      # customer-specific mapping
    lk = s.table("ap_invoices_all").column("invoice_type_lookup_code")
    assert lk.role == SemanticRole.LOOKUP and lk.lookup_type == "INVOICE TYPE"
    assert "STANDARD" in lk.allowed_values


def test_extracted_schema_flows_through_pipeline():
    """The whole point: an extracted real-EBS-shaped schema is a drop-in for the
    synthetic one -- graph joins resolve and the sampler builds valid SQL."""
    s = _schema()
    g = SchemaGraph(s)
    # header -> lines -> code_combinations bridge resolves
    path = g.join_path("ap_invoice_lines_all", "gl_code_combinations")
    assert path is not None and len(path) == 1
    import random
    for level in (1, 2, 3):
        ast, _ = QuerySampler(g, random.Random(0)).sample(level)
        assert validate_graph(ast, g).ok
