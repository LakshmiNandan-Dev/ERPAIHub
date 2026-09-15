"""EBS catalog extraction: raw data-dictionary rows -> a semantic, join-aware
Schema. The hard part is inferring FKs (EBS declares none) and flexfield/lookup
meaning, and producing a Schema the rest of the pipeline consumes unchanged."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm.extract import EbsExtractor, MockCatalog  # noqa: E402
from tinyllm.extract.catalog import CatalogSource, RawCodeObject, RawColumn  # noqa: E402
from tinyllm.extract.code_mining import mine_column_labels, mine_join_hints  # noqa: E402
from tinyllm.schema_graph import SchemaGraph  # noqa: E402
from tinyllm.schema_graph.types import SemanticRole  # noqa: E402
from tinyllm.sql_sampler import QuerySampler  # noqa: E402
from tinyllm.validate import validate_graph  # noqa: E402


def _schema():
    return EbsExtractor(MockCatalog()).extract()


def test_tables_and_multi_org():
    s = _schema()
    assert set(s.table_names) == {
        "ap_suppliers", "ap_invoices_all", "ap_invoice_lines_all",
        "gl_code_combinations", "ap_terms", "gl_journals_all", "gl_ledgers",
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


def test_foreign_key_mined_from_view_text():
    """terms_id -> ap_terms.term_id: the column name doesn't match the target's
    PK name, so naming-convention inference alone can't find it -- only the
    view's SQL (MockCatalog.code_objects()) reveals the join."""
    s = _schema()
    edges = {(fk.from_table, fk.from_column, fk.to_table, fk.to_column)
             for fk in s.foreign_keys}
    assert ("ap_invoices_all", "terms_id", "ap_terms", "term_id") in edges

    class _NoCode(MockCatalog):
        def code_objects(self):
            return []

    edges_without_mining = {
        (fk.from_table, fk.from_column, fk.to_table, fk.to_column)
        for fk in EbsExtractor(_NoCode()).extract().foreign_keys
    }
    assert ("ap_invoices_all", "terms_id", "ap_terms", "term_id") not in edges_without_mining


def test_foreign_key_mined_from_materialized_view_text():
    """The same terms_id -> ap_terms.term_id edge, mined from a MATERIALIZED
    VIEW this time (kind='materialized_view') rather than a plain view."""
    s = _schema()
    edges = {(fk.from_table, fk.from_column, fk.to_table, fk.to_column)
             for fk in s.foreign_keys}
    assert ("ap_invoices_all", "terms_id", "ap_terms", "term_id") in edges
    mv = next(o for o in MockCatalog().code_objects() if o.name == "ap_invoices_terms_mv")
    assert mv.kind == "materialized_view"


def test_foreign_key_mined_from_type_body_business_key_join():
    """gl_journals_all.ledger_name -> gl_ledgers.ledger_name: a join on a
    non-*_id business key. Naming-convention inference NEVER considers
    non-*_id columns at all (not just a name mismatch, structurally out of
    scope), so this is only ever discoverable from code -- here a TYPE BODY
    member function, mined via the regex fallback (sqlglot can't parse
    'TYPE BODY ... MEMBER FUNCTION ... BEGIN ... END' as one SQL statement)."""
    s = _schema()
    edges = {(fk.from_table, fk.from_column, fk.to_table, fk.to_column)
             for fk in s.foreign_keys}
    assert ("gl_journals_all", "ledger_name", "gl_ledgers", "ledger_name") in edges

    ty = next(o for o in MockCatalog().code_objects() if o.name == "gl_ledger_util_ty")
    assert ty.kind == "type_body"


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


# -- mine_join_hints in isolation: the sqlglot path (views -- clean single
#    SELECT statements) and the regex fallback (PL/SQL bodies sqlglot can't
#    parse), plus the "must resolve to two known tables" false-positive filter
_KNOWN = {"ap_invoices_all", "ap_terms", "ap_suppliers"}


def test_mine_join_hints_view_via_sqlglot():
    hints = mine_join_hints(
        [("ap_invoices_terms_v", "view",
          "SELECT i.invoice_id, t.name FROM ap_invoices_all i, ap_terms t "
          "WHERE i.terms_id = t.term_id")],
        _KNOWN,
    )
    assert [(h.from_table, h.from_column, h.to_table, h.to_column) for h in hints] == [
        ("ap_invoices_all", "terms_id", "ap_terms", "term_id"),
    ]


def test_mine_join_hints_plsql_via_regex_fallback():
    """A PL/SQL procedure body -- sqlglot can't parse BEGIN/END as one SQL
    statement, so this exercises the regex scan instead."""
    body = """
        PROCEDURE sync_terms IS
        BEGIN
            UPDATE ap_invoices_all i
               SET i.terms_id = (SELECT t.term_id FROM ap_terms t
                                   WHERE t.term_id = i.terms_id)
             WHERE EXISTS (
                SELECT 1 FROM ap_terms t WHERE i.terms_id = t.term_id
             );
        END sync_terms;
    """
    hints = mine_join_hints([("sync_terms", "procedure", body)], _KNOWN)
    assert ("ap_invoices_all", "terms_id", "ap_terms", "term_id") in {
        (h.from_table, h.from_column, h.to_table, h.to_column) for h in hints
    }


def test_mine_join_hints_drops_unresolved_predicates():
    """Record-field access and joins against tables outside the extracted
    catalog scope must NOT be guessed into edges."""
    text = (
        "SELECT l_rec.invoice_id FROM dual "            # l_rec isn't a real table
        "WHERE l_rec.status = other_schema_tbl.status"   # neither side is in `known`
    )
    assert mine_join_hints([("x", "function", text)], _KNOWN) == []


def test_mine_join_hints_ignores_self_joins():
    text = "SELECT a.invoice_id FROM ap_invoices_all a, ap_invoices_all b WHERE a.invoice_id = b.invoice_id"
    assert mine_join_hints([("x", "view", text)], _KNOWN) == []


# -- mine_column_labels: business-label aliases from view/mview SELECT-lists,
#    the same kind of thing FND flexfield/lookup metadata provides but
#    discoverable straight from how the customer's own views already label a
#    column -- confidently, only for a DIRECT column alias, no regex fallback
def test_mine_column_labels_qualified_and_unqualified_alias():
    objs = [
        ("ap_terms_v", "view",
         "SELECT t.term_id, t.name terms_name FROM ap_terms t"),          # qualified
        ("gl_cc_v", "materialized_view",
         "SELECT segment3 cost_center FROM gl_code_combinations"),        # unqualified, sole table
    ]
    labels = mine_column_labels(objs, _KNOWN | {"ap_terms", "gl_code_combinations"})
    assert labels["ap_terms"]["name"] == "terms name"
    assert labels["gl_code_combinations"]["segment3"] == "cost center"


def test_mine_column_labels_frequency_voting_with_alphabetical_tiebreak():
    objs = [
        ("v1", "view", "SELECT t.name label_a FROM ap_terms t"),
        ("v2", "view", "SELECT t.name label_b FROM ap_terms t"),
        ("v3", "view", "SELECT t.name label_a FROM ap_terms t"),   # label_a: 2 votes, wins
    ]
    labels = mine_column_labels(objs, _KNOWN | {"ap_terms"})
    assert labels["ap_terms"]["name"] == "label a"

    tied = [
        ("v1", "view", "SELECT t.name zeta FROM ap_terms t"),
        ("v2", "view", "SELECT t.name alpha FROM ap_terms t"),    # 1-1 tie -> alphabetically first
    ]
    assert mine_column_labels(tied, _KNOWN | {"ap_terms"})["ap_terms"]["name"] == "alpha"


def test_mine_column_labels_skips_non_column_and_identical_aliases():
    objs = [
        ("v1", "view", "SELECT UPPER(t.name) name_upper FROM ap_terms t"),  # not a bare column
        ("v2", "view", "SELECT t.term_id term_id FROM ap_terms t"),         # alias == column name
    ]
    assert mine_column_labels(objs, _KNOWN | {"ap_terms"}) == {}


def test_mine_column_labels_ignores_procedural_code():
    """No regex fallback for labels -- a package body's cursor SELECT with an
    aliased column is not mined (unlike joins, which do have a regex path)."""
    body = "PROCEDURE p IS BEGIN SELECT t.name terms_name INTO x FROM ap_terms t; END;"
    assert mine_column_labels([("p", "procedure", body)], _KNOWN | {"ap_terms"}) == {}


def test_flex_metadata_takes_priority_over_mined_label():
    """FND flexfield metadata is authoritative; a mined view alias only fills
    in where that metadata is absent -- confirmed via the full extractor."""
    s = _schema()
    seg2 = s.table("gl_code_combinations").column("segment2")
    assert seg2.business_label == "cost center"                 # from RawFlex, unchanged

    class _ConflictingLabelCatalog(MockCatalog):
        def code_objects(self):
            return super().code_objects() + [
                RawCodeObject(
                    "gl_cc_conflict_v", "view",
                    "SELECT segment2 department FROM gl_code_combinations",
                ),
            ]

    s2 = EbsExtractor(_ConflictingLabelCatalog()).extract()
    assert s2.table("gl_code_combinations").column("segment2").business_label == "cost center"


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
_SYNS = [                                    # (synonym_name, table_owner, table_name, owner)
    ("AP_INVOICES_ALL", "AP", "AP_INVOICES_ALL", "APPS"),
    ("AP_SUPPLIERS", "AP", "PO_VENDORS", "APPS"),    # canonical name != base table
    ("GL_CODE_COMBINATIONS", "GL", "GL_CODE_COMBINATIONS", "APPS"),
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
        if "FROM dual" in sql:               # connected-user check
            self._rows = [("APPS",)]
        elif "all_tab_columns" in sql:
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


# -- PUBLIC synonyms matter as much as APPS-owned ones: confirmed against a
#    real instance that APPS-owned views reference tables fully unqualified
#    ("FROM AP_INVOICES_ALL", no owner, no APPS-private synonym) -- only
#    resolvable via a PUBLIC synonym. APPS-owned still wins when both exist.
class _SynPrecedenceCursor:
    def __init__(self, syn_rows):
        self._syn_rows = syn_rows
        self._rows: list = []

    def execute(self, sql, bind=None):
        if "FROM dual" in sql:
            self._rows = [("APPS",)]
        elif "all_synonyms" in sql:
            self._rows = self._syn_rows
        elif "all_tables" in sql:
            self._rows = [("XX", "XX_BASE_TABLE")]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


def test_oracle_catalog_public_synonym_resolves_bare_name():
    from tinyllm.extract.catalog import OracleCatalog
    cat = OracleCatalog(_SynPrecedenceCursor(
        [("XX_PUBLIC_NAME", "XX", "XX_BASE_TABLE", "PUBLIC")]))
    assert cat.tables() == ["xx_public_name"]      # not owner-qualified "xx.xx_base_table"


@pytest.mark.parametrize("row_order", [
    [("XX_PUBLIC_NAME", "XX", "XX_BASE_TABLE", "PUBLIC"), ("XX_APPS_NAME", "XX", "XX_BASE_TABLE", "APPS")],
    [("XX_APPS_NAME", "XX", "XX_BASE_TABLE", "APPS"), ("XX_PUBLIC_NAME", "XX", "XX_BASE_TABLE", "PUBLIC")],
])
def test_oracle_catalog_apps_synonym_wins_over_public_regardless_of_row_order(row_order):
    from tinyllm.extract.catalog import OracleCatalog
    cat = OracleCatalog(_SynPrecedenceCursor(row_order))
    assert cat.tables() == ["xx_apps_name"]


class _SelfOwnedTableCursor:
    """A table owned by the CONNECTED user, with no synonym of any kind."""

    def __init__(self):
        self._rows: list = []

    def execute(self, sql, bind=None):
        if "FROM dual" in sql:
            self._rows = [("APPS",)]
        elif "all_synonyms" in sql:
            self._rows = []                          # no synonym, private or public
        elif "all_tables" in sql:
            self._rows = [("APPS", "APPS_OWN_TABLE")]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


def test_oracle_catalog_connected_users_own_table_needs_no_synonym():
    """Oracle resolves an unqualified name in your OWN schema before ever
    checking a synonym -- confirmed needed extracting AS apps itself: 1,153 of
    APPS's own tables had no synonym of either kind yet are trivially bare-
    referenceable, and were falling back to a needless "apps.table_name"."""
    from tinyllm.extract.catalog import OracleCatalog
    cat = OracleCatalog(_SelfOwnedTableCursor())
    assert cat.tables() == ["apps_own_table"]        # not "apps.apps_own_table"


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


# -- OracleCatalog.code_objects(): ALL_VIEWS + ALL_SOURCE (one row per line,
#    concatenated by (name, type) into a single body) + ALL_TRIGGERS, and the
#    join it mines flowing through EbsExtractor exactly like the mock's does --
_CODE_TABLES = [("AP", "AP_INVOICES_ALL"), ("AP", "AP_TERMS")]
_CODE_SYNS = [("AP_INVOICES_ALL", "AP", "AP_INVOICES_ALL", "APPS"), ("AP_TERMS", "AP", "AP_TERMS", "APPS")]
_CODE_COLS = [
    ("AP", "AP_INVOICES_ALL", "INVOICE_ID", "NUMBER", "N"),
    ("AP", "AP_INVOICES_ALL", "TERMS_ID", "NUMBER", "Y"),
    ("AP", "AP_TERMS", "TERM_ID", "NUMBER", "N"),
    ("AP", "AP_TERMS", "NAME", "VARCHAR2", "Y"),
]
_CODE_PKS = [("AP", "AP_INVOICES_ALL", "INVOICE_ID"), ("AP", "AP_TERMS", "TERM_ID")]
_VIEWS = [
    ("AP_INVOICES_TERMS_V",
     "SELECT i.invoice_id, t.name FROM ap_invoices_all i, ap_terms t "
     "WHERE i.terms_id = t.term_id"),
]
_MVIEWS = [
    ("AP_INVOICES_TERMS_MV",
     "SELECT i.invoice_id, t.name FROM ap_invoices_all i, ap_terms t "
     "WHERE i.terms_id = t.term_id"),
]
_SOURCE = [                              # ALL_SOURCE: one row per line, in order
    ("AP_TERMS_PKG", "PACKAGE BODY", 1, "PROCEDURE sync_terms IS\n"),
    ("AP_TERMS_PKG", "PACKAGE BODY", 2, "BEGIN\n"),
    ("AP_TERMS_PKG", "PACKAGE BODY", 3, "  UPDATE ap_invoices_all i SET i.terms_id = i.terms_id\n"),
    ("AP_TERMS_PKG", "PACKAGE BODY", 4,
     "   WHERE EXISTS (SELECT 1 FROM ap_terms t WHERE i.terms_id = t.term_id);\n"),
    ("AP_TERMS_PKG", "PACKAGE BODY", 5, "END sync_terms;\n"),
    ("AP_TERMS_TY", "TYPE BODY", 1, "TYPE BODY ap_terms_ty IS\n"),
    ("AP_TERMS_TY", "TYPE BODY", 2, "  MEMBER FUNCTION nm(p_id NUMBER) RETURN VARCHAR2 IS\n"),
    ("AP_TERMS_TY", "TYPE BODY", 3, "    v VARCHAR2(60);\n"),
    ("AP_TERMS_TY", "TYPE BODY", 4, "  BEGIN\n"),
    ("AP_TERMS_TY", "TYPE BODY", 5,
     "    SELECT t.name INTO v FROM ap_invoices_all i, ap_terms t\n"),
    ("AP_TERMS_TY", "TYPE BODY", 6, "     WHERE i.invoice_id = p_id AND i.terms_id = t.term_id;\n"),
    ("AP_TERMS_TY", "TYPE BODY", 7, "    RETURN v;\n"),
    ("AP_TERMS_TY", "TYPE BODY", 8, "  END nm;\n"),
    ("AP_TERMS_TY", "TYPE BODY", 9, "END;\n"),
]
_TRIGGERS = [("TRG_AP_TERMS_CHECK", "BEGIN NULL; END;")]


class _CodeFakeCursor:
    """Same shape as _FakeCursor, plus ALL_VIEWS/ALL_MVIEWS/ALL_SOURCE/ALL_TRIGGERS routing."""

    def __init__(self):
        self._rows: list = []

    def execute(self, sql, bind=None):
        if "FROM dual" in sql:
            self._rows = [("APPS",)]
        elif "all_mviews" in sql:
            self._rows = _MVIEWS
        elif "all_views" in sql:
            self._rows = _VIEWS
        elif "all_source" in sql:
            self._rows = _SOURCE
        elif "all_triggers" in sql:
            self._rows = _TRIGGERS
        elif "all_tab_columns" in sql:
            self._rows = _CODE_COLS
        elif "constraint_type = 'P'" in sql:
            self._rows = _CODE_PKS
        elif "constraint_type = 'R'" in sql:
            self._rows = []
        elif "all_synonyms" in sql:
            self._rows = _CODE_SYNS         # APPS synonyms -- code refers to tables unqualified
        elif "all_tables" in sql:
            self._rows = _CODE_TABLES
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


def test_oracle_catalog_code_objects_assembled_from_views_source_triggers():
    from tinyllm.extract.catalog import OracleCatalog
    objs = {o.name: o for o in OracleCatalog(_CodeFakeCursor()).code_objects()}

    view = objs["ap_invoices_terms_v"]
    assert isinstance(view, RawCodeObject) and view.kind == "view"
    assert "i.terms_id = t.term_id" in view.text

    mv = objs["ap_invoices_terms_mv"]
    assert mv.kind == "materialized_view"
    assert "i.terms_id = t.term_id" in mv.text

    pkg = objs["ap_terms_pkg"]                    # per-line rows concatenated IN ORDER
    assert pkg.kind == "package_body"
    assert pkg.text.index("BEGIN") < pkg.text.index("UPDATE") < pkg.text.index("END sync_terms")

    ty = objs["ap_terms_ty"]                       # TYPE BODY rows concatenated IN ORDER too
    assert ty.kind == "type_body"
    assert ty.text.index("BEGIN") < ty.text.index("SELECT") < ty.text.index("END nm")

    assert objs["trg_ap_terms_check"].kind == "trigger"


def test_oracle_catalog_mined_join_flows_through_extractor():
    from tinyllm.extract.catalog import OracleCatalog
    s = EbsExtractor(OracleCatalog(_CodeFakeCursor())).extract()
    edges = {(fk.from_table, fk.from_column, fk.to_table, fk.to_column)
             for fk in s.foreign_keys}
    assert ("ap_invoices_all", "terms_id", "ap_terms", "term_id") in edges


# -- unique-index PK fallback: confirmed against a real EBS instance that core
#    seed tables (ap_invoices_all, ap_suppliers, gl_code_combinations, ...)
#    have NO PRIMARY KEY constraint at all -- only a unique index. Without
#    this fallback, naming-convention FK inference finds almost nothing real.
_UIDX_TABLES = [("AP", "AP_SUPPLIERS"), ("AP", "AP_INVOICES_ALL"), ("GL", "GL_CODE_COMBINATIONS")]
_UIDX_SYNS = [("AP_SUPPLIERS", "AP", "AP_SUPPLIERS", "APPS"),
              ("AP_INVOICES_ALL", "AP", "AP_INVOICES_ALL", "APPS"),
              ("GL_CODE_COMBINATIONS", "GL", "GL_CODE_COMBINATIONS", "APPS")]
_UIDX_COLS = [
    ("AP", "AP_SUPPLIERS", "VENDOR_ID", "NUMBER", "N"),
    ("AP", "AP_SUPPLIERS", "SEGMENT1", "VARCHAR2", "Y"),
    ("AP", "AP_INVOICES_ALL", "INVOICE_ID", "NUMBER", "N"),
    ("AP", "AP_INVOICES_ALL", "VENDOR_ID", "NUMBER", "Y"),
    ("AP", "AP_INVOICES_ALL", "DOC_SEQUENCE_ID", "NUMBER", "Y"),
    ("AP", "AP_INVOICES_ALL", "DOC_SEQUENCE_VALUE", "NUMBER", "Y"),
    ("GL", "GL_CODE_COMBINATIONS", "CODE_COMBINATION_ID", "NUMBER", "N"),
]
_UIDX_PKS = [                             # real PK CONSTRAINT -- only GL_CODE_COMBINATIONS has one
    ("GL", "GL_CODE_COMBINATIONS", "CODE_COMBINATION_ID"),
]
_UNIQUE_INDEXES = [               # (owner, table, index_name, column_name, position)
    ("AP", "AP_SUPPLIERS", "AP_SUPPLIERS_U2", "SEGMENT1", 1),      # alternate key, NOT chosen
    ("AP", "AP_SUPPLIERS", "AP_SUPPLIERS_U1", "VENDOR_ID", 1),     # single-col, alphabetically first
    ("AP", "AP_INVOICES_ALL", "AP_INVOICES_U3", "DOC_SEQUENCE_ID", 1),
    ("AP", "AP_INVOICES_ALL", "AP_INVOICES_U3", "DOC_SEQUENCE_VALUE", 2),   # 2-col -> loses to U1
    ("AP", "AP_INVOICES_ALL", "AP_INVOICES_U1", "INVOICE_ID", 1),          # 1-col -> chosen
    # GL_CODE_COMBINATIONS has a real PK constraint above; this must be IGNORED
    ("GL", "GL_CODE_COMBINATIONS", "GL_CODE_COMBINATIONS_U2", "SEGMENT1", 1),
]


class _UniqueIndexFakeCursor:
    def __init__(self):
        self._rows: list = []

    def execute(self, sql, bind=None):
        if "FROM dual" in sql:
            self._rows = [("APPS",)]
        elif "all_indexes" in sql:
            self._rows = _UNIQUE_INDEXES
        elif "all_tab_columns" in sql:
            self._rows = _UIDX_COLS
        elif "constraint_type = 'P'" in sql:
            self._rows = _UIDX_PKS
        elif "constraint_type = 'R'" in sql:
            self._rows = []
        elif "all_synonyms" in sql:
            self._rows = _UIDX_SYNS
        elif "all_tables" in sql:
            self._rows = _UIDX_TABLES
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


def test_oracle_catalog_pk_falls_back_to_unique_index():
    from tinyllm.extract.catalog import OracleCatalog
    cat = OracleCatalog(_UniqueIndexFakeCursor())

    # single-column unique indexes tie on length -> alphabetically-first name wins
    # (AP_SUPPLIERS_U1 < AP_SUPPLIERS_U2), which is also the real EBS convention
    assert cat.primary_key("ap_suppliers") == ["vendor_id"]
    # multi-column index loses to the single-column one regardless of name
    assert cat.primary_key("ap_invoices_all") == ["invoice_id"]
    # a real PK constraint is never overridden by a unique index
    assert cat.primary_key("gl_code_combinations") == ["code_combination_id"]


def test_oracle_catalog_pk_fallback_unblocks_naming_inference():
    """The point: without real PKs, ap_invoices_all.vendor_id can't link to
    ap_suppliers at all (pk_owner never registers 'vendor_id' -> ap_suppliers).
    With the unique-index fallback, the standard join is inferred correctly."""
    from tinyllm.extract.catalog import OracleCatalog
    s = EbsExtractor(OracleCatalog(_UniqueIndexFakeCursor())).extract()
    edges = {(fk.from_table, fk.from_column, fk.to_table, fk.to_column)
             for fk in s.foreign_keys}
    assert ("ap_invoices_all", "vendor_id", "ap_suppliers", "vendor_id") in edges
