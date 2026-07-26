"""Schema-aware repair: fix the model's semantic slips (wrong flexfield segment,
out-of-domain lookup value) deterministically from the schema + question, and
leave correct/ambiguous SQL untouched."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm.decode import schema_repair  # noqa: E402
from tinyllm.extract import EbsExtractor, MockCatalog  # noqa: E402

_SCHEMA = EbsExtractor(MockCatalog()).extract()   # segment2 = "cost center", etc.


def test_flexfield_segment_repair():
    sql = ("SELECT gcc.segment5, SUM(aia.invoice_amount) AS s FROM ap_invoices_all aia "
           "JOIN ap_invoice_lines_all ail ON ail.invoice_id = aia.invoice_id "
           "JOIN gl_code_combinations gcc ON ail.code_combination_id = gcc.code_combination_id "
           "GROUP BY gcc.segment5")
    fixed = schema_repair(sql, "total invoice amount by cost center", _SCHEMA)
    assert "segment2" in fixed and "segment5" not in fixed      # cost center -> segment2


def test_lookup_value_repair():
    sql = "SELECT aia.invoice_id FROM ap_invoices_all aia WHERE aia.invoice_type_lookup_code = 'PAID'"
    fixed = schema_repair(sql, "invoices where invoice type lookup code is standard", _SCHEMA)
    assert "'STANDARD'" in fixed and "'PAID'" not in fixed       # PAID not allowed -> STANDARD


def test_repair_is_noop_when_correct_or_ambiguous():
    good = "SELECT aia.invoice_id FROM ap_invoices_all aia WHERE aia.invoice_type_lookup_code = 'CREDIT'"
    assert schema_repair(good, "credit invoices", _SCHEMA) == good       # already valid value
    seg = "SELECT gcc.segment5 FROM gl_code_combinations gcc"
    assert schema_repair(seg, "list code combinations", _SCHEMA) == seg  # no label in question


def test_org_filter_repair():
    sql = "SELECT aia.invoice_id FROM ap_invoices_all aia WHERE aia.org_id = 101"
    for q in ("invoices for operating unit 305", "invoices for org 305",
              "invoices for ou 305", "invoices in operating unit 305"):
        assert "aia.org_id = 305" in schema_repair(sql, q, _SCHEMA), q


def test_org_filter_repair_noop_when_unnamed():
    sql = "SELECT aia.invoice_id FROM ap_invoices_all aia WHERE aia.org_id = 101"
    assert schema_repair(sql, "list all invoices", _SCHEMA) == sql       # no org number named
    # a year in the question must NOT be mistaken for an org value
    dated = "SELECT aia.invoice_id FROM ap_invoices_all aia WHERE EXTRACT(YEAR FROM aia.invoice_date) = 2024"
    assert schema_repair(dated, "invoices in 2024", _SCHEMA) == dated
