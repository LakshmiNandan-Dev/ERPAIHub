"""EBS catalog extraction: raw data-dictionary rows -> a semantic, join-aware
Schema. The hard part is inferring FKs (EBS declares none) and flexfield/lookup
meaning, and producing a Schema the rest of the pipeline consumes unchanged."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm.extract import EbsExtractor, MockCatalog  # noqa: E402
from tinyllm.extract.catalog import CatalogSource, RawColumn  # noqa: E402
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


# -- edge cases the AP+GL mock doesn't exercise: a composite-PK child whose FK
#    column is part of its own PK, plus role heuristics when no lookup/flex
#    metadata is configured (the shape a live pull produces today) -------------
class _EdgeCaseCatalog(CatalogSource):
    _COLUMNS = {
        "ap_invoice_distributions_all": [
            RawColumn("invoice_id", "NUMBER", False),               # FK *and* PK member
            RawColumn("distribution_line_number", "NUMBER", False),
            RawColumn("amount", "NUMBER"),
            RawColumn("internet_sessions", "NUMBER"),               # 'net' substring, NOT an amount
            RawColumn("posted_flag", "VARCHAR2"),                   # flag, no lookup metadata
        ],
        "ap_invoices_all": [RawColumn("invoice_id", "NUMBER", False)],
    }
    _PK = {
        "ap_invoice_distributions_all": ["invoice_id", "distribution_line_number"],
        "ap_invoices_all": ["invoice_id"],
    }

    def tables(self):
        return list(self._COLUMNS)

    def columns(self, table):
        return list(self._COLUMNS[table])

    def primary_key(self, table):
        return list(self._PK.get(table, []))


def test_composite_pk_child_still_links_to_parent():
    s = EbsExtractor(_EdgeCaseCatalog()).extract()
    edges = {(fk.from_table, fk.from_column, fk.to_table, fk.to_column)
             for fk in s.foreign_keys}
    # invoice_id is part of the child's COMPOSITE PK yet is still a real FK to the header
    assert ("ap_invoice_distributions_all", "invoice_id",
            "ap_invoices_all", "invoice_id") in edges
    # ...and the header's own invoice_id must not self-link
    assert not any(fk.from_table == fk.to_table for fk in s.foreign_keys)


def test_role_heuristics_without_lookup_metadata():
    dist = EbsExtractor(_EdgeCaseCatalog()).extract().table("ap_invoice_distributions_all")
    assert dist.column("posted_flag").role == SemanticRole.CODE       # flag -> CODE fallback
    assert dist.column("amount").role == SemanticRole.AMOUNT
    assert dist.column("internet_sessions").role is None              # 'net' is not a token


# -- live OracleCatalog (HYBRID, no Oracle): a fake cursor returns canned set-
#    based rows so the table list (ALL_TABLES) + canonical APPS-synonym rename +
#    skip-list + caching are all proven without a database --------------------
_TABLES = [                                  # ALL_TABLES rows: (owner, table_name)
    ("AP", "AP_INVOICES_ALL"),
    ("AP", "PO_VENDORS"),                    # base table; APPS renames it ap_suppliers
    ("GL", "GL_CODE_COMBINATIONS"),
    ("XXCUST", "XX_CUSTOM_TABLE"),           # custom: NO synonym -> kept owner-qualified
    ("AP", "AP_STUFF$TMP"),                  # technical ('$') -> skipped
    ("GL", "GL_BALANCES_BAK"),               # backup suffix -> skipped
]
_SYNS = [                                    # (synonym_name, table_owner, table_name)
    ("AP_INVOICES_ALL", "AP", "AP_INVOICES_ALL"),
    ("AP_SUPPLIERS", "AP", "PO_VENDORS"),    # canonical name != base table
    ("GL_CODE_COMBINATIONS", "GL", "GL_CODE_COMBINATIONS"),
]
_COLS = [                                    # (owner, table, column, type, nullable)
    ("AP", "AP_INVOICES_ALL", "INVOICE_ID", "NUMBER", "N"),
    ("AP", "AP_INVOICES_ALL", "VENDOR_ID", "NUMBER", "Y"),
    ("AP", "AP_INVOICES_ALL", "INVOICE_AMOUNT", "NUMBER", "Y"),
    ("AP", "PO_VENDORS", "VENDOR_ID", "NUMBER", "N"),
    ("AP", "PO_VENDORS", "VENDOR_NAME", "VARCHAR2", "Y"),
    ("GL", "GL_CODE_COMBINATIONS", "CODE_COMBINATION_ID", "NUMBER", "N"),
    ("GL", "GL_CODE_COMBINATIONS", "SEGMENT1", "VARCHAR2", "Y"),
    ("XXCUST", "XX_CUSTOM_TABLE", "CUSTOM_ID", "NUMBER", "N"),
    ("XXCUST", "XX_CUSTOM_TABLE", "NOTE", "VARCHAR2", "Y"),
    ("AP", "AP_STUFF$TMP", "X", "NUMBER", "Y"),   # skipped table -> columns dropped
]
_PKS = [
    ("AP", "AP_INVOICES_ALL", "INVOICE_ID"),
    ("AP", "PO_VENDORS", "VENDOR_ID"),
    ("GL", "GL_CODE_COMBINATIONS", "CODE_COMBINATION_ID"),
    ("XXCUST", "XX_CUSTOM_TABLE", "CUSTOM_ID"),
]


class _FakeCursor:
    """Routes each OracleCatalog bulk query to canned rows by a distinctive token."""

    def __init__(self):
        self._rows: list = []
        self.table_query_count = 0

    def execute(self, sql, bind=None):
        if "all_tab_columns" in sql:
            self._rows = _COLS
        elif "constraint_type = 'P'" in sql:
            self._rows = _PKS
        elif "constraint_type = 'R'" in sql:
            self._rows = []                  # EBS declares no FKs
        elif "all_synonyms" in sql:          # the synonym overlay
            self._rows = _SYNS
        elif "all_tables" in sql:            # the table-list query
            self.table_query_count += 1
            self._rows = _TABLES
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


def test_oracle_catalog_hybrid_naming_skip_and_caching():
    from tinyllm.extract.catalog import OracleCatalog
    cur = _FakeCursor()
    cat = OracleCatalog(cur)

    # base PO_VENDORS surfaced under its canonical APPS synonym; the synonym-less
    # custom table is kept (owner-qualified); '$' and *_BAK tables are skipped
    assert cat.tables() == [
        "ap_invoices_all", "ap_suppliers", "gl_code_combinations", "xxcust.xx_custom_table"
    ]
    # the renamed base table's columns come back under the canonical name
    cols = {c.name: c for c in cat.columns("ap_suppliers")}
    assert set(cols) == {"vendor_id", "vendor_name"}
    assert cols["vendor_id"].nullable is False
    # synonym-less custom table IS captured (not missed) under owner.table
    assert {c.name for c in cat.columns("xxcust.xx_custom_table")} == {"custom_id", "note"}
    assert cat.primary_key("ap_suppliers") == ["vendor_id"]
    # technical tables never appear
    assert not any("$" in t or t.endswith("_bak") for t in cat.tables())
    # bulk read happens ONCE and is cached -- not re-queried per accessor call
    cat.columns("ap_invoices_all"); cat.primary_key("ap_suppliers"); cat.tables()
    assert cur.table_query_count == 1


def test_extra_owners_are_sanitized_into_scope():
    """Custom owners are inlined into the scope subquery, so they must be stripped
    to bare identifiers (no SQL injection)."""
    from tinyllm.extract.catalog import OracleCatalog
    cat = OracleCatalog(_FakeCursor(), extra_owners=["XXCUST", "evil'; DROP TABLE x--"])
    scope = cat._sql["tables"]
    assert "'XXCUST'" in scope                 # clean owner inlined
    assert "EVILDROPTABLEX" in scope          # non-identifier chars removed, upper-cased
    assert "DROP TABLE" not in scope and "--" not in scope and "';" not in scope


def test_oracle_catalog_flows_through_extractor():
    """End to end on the hybrid adapter: extract() infers the join graph from the
    bulk-read metadata exactly as it does for the mock."""
    from tinyllm.extract.catalog import OracleCatalog
    s = EbsExtractor(OracleCatalog(_FakeCursor())).extract()
    assert set(s.table_names) == {
        "ap_invoices_all", "ap_suppliers", "gl_code_combinations", "xxcust.xx_custom_table"
    }
    edges = {(fk.from_table, fk.from_column, fk.to_table, fk.to_column)
             for fk in s.foreign_keys}
    assert ("ap_invoices_all", "vendor_id", "ap_suppliers", "vendor_id") in edges
