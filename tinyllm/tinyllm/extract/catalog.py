"""Catalog source: where raw EBS metadata comes from.

`CatalogSource` is the seam between *reading* an Oracle instance (read-only, via
the data dictionary) and the *mapping* logic in `extractor.py`. The real adapter
(`OracleCatalog`) issues the documented SQL below against `ALL_*`/`FND_*`; the
`MockCatalog` returns canned rows for the same shape, so the extractor's logic is
fully testable with no database. Nothing here leaves the customer network.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawColumn:
    name: str
    data_type: str
    nullable: bool = True


@dataclass
class RawFk:
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass
class RawFlex:
    table: str
    column: str
    business_label: str          # from FND_ID_FLEX_SEGMENTS (CUSTOMER-SPECIFIC)


@dataclass
class RawLookup:
    table: str
    column: str
    lookup_type: str
    values: tuple = ()


class CatalogSource:
    """Interface every catalog reader implements (real Oracle or mock)."""

    def tables(self) -> list[str]: raise NotImplementedError
    def columns(self, table: str) -> list[RawColumn]: raise NotImplementedError
    def primary_key(self, table: str) -> list[str]: raise NotImplementedError
    def foreign_keys(self) -> list[RawFk]: return []          # EBS often declares none
    def flex_segments(self) -> list[RawFlex]: return []
    def lookups(self) -> list[RawLookup]: return []


# -- the real adapter's SQL (HYBRID; read-only data dictionary + FND setup) ----
#
# Scope = ALL tables in ALL schemas that are LICENSED ('I') or SHARED-LICENSE
# ('S') in fnd_product_installations (status 'N' = not installed is excluded),
# PLUS any extra owners you name (e.g. custom CEMLI schemas like XXxx).
#
# HYBRID naming: the table LIST is driven off ALL_TABLES (so synonym-less /
# custom tables are NOT missed), then each table is renamed to its canonical
# APPS synonym when one exists (so generated SQL runs unqualified as APPS), and
# kept owner-qualified ("owner.table") otherwise. Technical tables (names with
# '$', plus an optional backup-suffix list) are skipped. All reads are BULK /
# set-based and cached -- a handful of queries for a ~20k-table instance, not one
# round-trip per table. (Needs a read-only account with dictionary/catalog
# access, e.g. SELECT_CATALOG_ROLE, so the ALL_* views show every owner.)

# in-scope owners: licensed + shared products from the FND install table
_SCOPE_BASE = (
    "SELECT u.oracle_username AS owner "
    "FROM fnd_product_installations i "
    "JOIN fnd_oracle_userid u ON u.oracle_id = i.oracle_id "
    "WHERE i.status IN ('I', 'S')"
)
_DEFAULT_SKIP_SUFFIXES = ("_BAK", "_BACKUP", "_BACK", "_OLD")


def _queries(scope: str) -> dict[str, str]:
    """The five bulk queries, parameterized by the in-scope-owner subquery."""
    return {
        # table LIST from ALL_TABLES (everything real in the in-scope owners)
        "tables": f"""
            SELECT t.owner, t.table_name
              FROM all_tables t
             WHERE t.owner IN ({scope})
               AND t.table_name NOT LIKE '%$%'
        """,
        # APPS synonym overlay: (owner, table) -> canonical name
        "synonyms": f"""
            SELECT s.synonym_name, s.table_owner, s.table_name
              FROM all_synonyms s
             WHERE s.owner = 'APPS'
               AND s.table_owner IN ({scope})
        """,
        # ALL columns for the in-scope owners, keyed by base (owner, table)
        "columns": f"""
            SELECT c.owner, c.table_name, c.column_name, c.data_type, c.nullable
              FROM all_tab_columns c
             WHERE c.owner IN ({scope})
             ORDER BY c.owner, c.table_name, c.column_id
        """,
        # ALL primary-key columns, keyed by base (owner, table)
        "primary_key": f"""
            SELECT con.owner, con.table_name, cc.column_name
              FROM all_constraints con
              JOIN all_cons_columns cc
                ON cc.owner = con.owner AND cc.constraint_name = con.constraint_name
             WHERE con.constraint_type = 'P'
               AND con.owner IN ({scope})
        """,
        # declared FKs (rare in EBS); both ends as base (owner, table)
        "foreign_keys": f"""
            SELECT fc.owner, fc.table_name, fcc.column_name,
                   pc.owner, pc.table_name, pcc.column_name
              FROM all_constraints fc
              JOIN all_cons_columns fcc
                ON fcc.owner = fc.owner AND fcc.constraint_name = fc.constraint_name
              JOIN all_constraints pc
                ON pc.owner = fc.r_owner AND pc.constraint_name = fc.r_constraint_name
              JOIN all_cons_columns pcc
                ON pcc.owner = pc.owner AND pcc.constraint_name = pc.constraint_name
               AND pcc.position = fcc.position
             WHERE fc.constraint_type = 'R'
               AND fc.owner IN ({scope})
        """,
    }


# the default queries (no extra owners) -- handy for reference/inspection
ORACLE_SQL = _queries(_SCOPE_BASE)


class OracleCatalog(CatalogSource):
    """Real adapter (HYBRID): the table list is read from ALL_TABLES for every
    licensed/shared (+ extra) owner, then renamed to its canonical APPS synonym
    when one exists, else kept owner-qualified. BULK set-based reads, cached once.

    The grouping/renaming/caching logic is tested via a fake cursor in
    test_extract; the live SQL still needs a real read-only EBS account (with
    dictionary access) to confirm against a given instance. Flexfield/lookup
    enrichment for live extraction is a documented follow-up -- the extractor
    degrades gracefully when those are absent."""

    def __init__(self, cursor, extra_owners=(), skip_suffixes=_DEFAULT_SKIP_SUFFIXES):
        self.cur = cursor
        self._extra = [self._ident(o) for o in extra_owners if self._ident(o)]
        self._skip_suffixes = tuple(s.upper() for s in skip_suffixes)
        self._sql = _queries(self._scope())
        self._canon: dict[tuple, str] = {}        # (OWNER, TABLE) -> canonical name
        self._tables: list[str] | None = None
        self._cols: dict[str, list[RawColumn]] = {}
        self._pk: dict[str, list[str]] = {}
        self._fks: list[RawFk] | None = None

    @staticmethod
    def _ident(owner: str) -> str:
        """Sanitize an owner name to a bare SQL identifier (it is inlined)."""
        return "".join(ch for ch in owner.upper() if ch.isalnum() or ch == "_")

    def _scope(self) -> str:
        sql = _SCOPE_BASE
        for o in self._extra:
            sql += f" UNION ALL SELECT '{o}' AS owner FROM dual"
        return sql

    def _skip(self, table_name: str) -> bool:
        tu = table_name.upper()
        return "$" in tu or tu.endswith(self._skip_suffixes)

    def _load(self) -> None:
        if self._tables is not None:
            return
        # 1. synonym overlay first: base (owner, table) -> canonical APPS name
        self.cur.execute(self._sql["synonyms"])
        syn = {(o.upper(), t.upper()): name.lower()
               for name, o, t in self.cur.fetchall()}
        # 2. table list from ALL_TABLES; canonical = synonym name, else owner.table
        self.cur.execute(self._sql["tables"])
        for owner, tname in self.cur.fetchall():
            if self._skip(tname):
                continue
            key = (owner.upper(), tname.upper())
            self._canon[key] = syn.get(key, f"{owner}.{tname}".lower())
        self._tables = sorted(set(self._canon.values()))
        # 3. columns / 4. PKs, mapped from base (owner, table) to canonical
        self.cur.execute(self._sql["columns"])
        for owner, tname, col, dtype, nullable in self.cur.fetchall():
            c = self._canon.get((owner.upper(), tname.upper()))
            if c:
                self._cols.setdefault(c, []).append(
                    RawColumn(col.lower(), dtype, nullable == "Y"))
        self.cur.execute(self._sql["primary_key"])
        for owner, tname, col in self.cur.fetchall():
            c = self._canon.get((owner.upper(), tname.upper()))
            if c:
                self._pk.setdefault(c, []).append(col.lower())

    def tables(self):
        self._load()
        return list(self._tables)

    def columns(self, table):
        self._load()
        return list(self._cols.get(table, []))

    def primary_key(self, table):
        self._load()
        return list(self._pk.get(table, []))

    def foreign_keys(self):
        self._load()
        if self._fks is None:                 # declared FKs optional; cached like the rest
            try:
                self.cur.execute(self._sql["foreign_keys"])
                out = []
                for fo, ft, fcol, po, pt, pcol in self.cur.fetchall():
                    a = self._canon.get((fo.upper(), ft.upper()))
                    b = self._canon.get((po.upper(), pt.upper()))
                    if a and b:
                        out.append(RawFk(a, fcol.lower(), b, pcol.lower()))
                self._fks = out
            except Exception:
                self._fks = []
        return list(self._fks)


# -- a small AP + GL mock instance (the spec's proposed starter modules) ------
class MockCatalog(CatalogSource):
    """Simulates a tiny EBS: AP invoices/suppliers + the GL accounting flexfield.
    Declares ZERO foreign keys (as real EBS usually does) -- the extractor must
    infer the join graph from naming conventions."""

    _COLUMNS: dict[str, list[RawColumn]] = {
        "ap_suppliers": [
            RawColumn("vendor_id", "NUMBER", False),
            RawColumn("vendor_name", "VARCHAR2"),
            RawColumn("vendor_number", "VARCHAR2"),
            RawColumn("creation_date", "DATE"),
        ],
        "ap_invoices_all": [
            RawColumn("invoice_id", "NUMBER", False),
            RawColumn("vendor_id", "NUMBER"),
            RawColumn("org_id", "NUMBER"),
            RawColumn("invoice_num", "VARCHAR2"),
            RawColumn("invoice_date", "DATE"),
            RawColumn("invoice_amount", "NUMBER"),
            RawColumn("invoice_type_lookup_code", "VARCHAR2"),
            RawColumn("payment_status_flag", "VARCHAR2"),
        ],
        "ap_invoice_lines_all": [
            RawColumn("invoice_line_id", "NUMBER", False),
            RawColumn("invoice_id", "NUMBER"),
            RawColumn("line_number", "NUMBER"),
            RawColumn("amount", "NUMBER"),
            RawColumn("code_combination_id", "NUMBER"),
            RawColumn("org_id", "NUMBER"),
        ],
        "gl_code_combinations": [
            RawColumn("code_combination_id", "NUMBER", False),
            RawColumn("segment1", "VARCHAR2"),
            RawColumn("segment2", "VARCHAR2"),
            RawColumn("segment3", "VARCHAR2"),
            RawColumn("segment4", "VARCHAR2"),
            RawColumn("segment5", "VARCHAR2"),
        ],
    }
    _PK = {
        "ap_suppliers": ["vendor_id"],
        "ap_invoices_all": ["invoice_id"],
        "ap_invoice_lines_all": ["invoice_line_id"],
        "gl_code_combinations": ["code_combination_id"],
    }
    _FLEX = [  # the customer's Accounting Flexfield segment labels
        RawFlex("gl_code_combinations", "segment1", "company"),
        RawFlex("gl_code_combinations", "segment2", "cost center"),
        RawFlex("gl_code_combinations", "segment3", "account"),
        RawFlex("gl_code_combinations", "segment4", "product"),
        RawFlex("gl_code_combinations", "segment5", "intercompany"),
    ]
    _LOOKUPS = [
        RawLookup("ap_invoices_all", "invoice_type_lookup_code", "INVOICE TYPE",
                  ("STANDARD", "CREDIT", "PREPAYMENT", "MIXED")),
        RawLookup("ap_invoices_all", "payment_status_flag", "PAYMENT STATUS",
                  ("Y", "N", "P")),
    ]

    def tables(self):
        return list(self._COLUMNS)

    def columns(self, table):
        return list(self._COLUMNS[table])

    def primary_key(self, table):
        return list(self._PK.get(table, []))

    def flex_segments(self):
        return list(self._FLEX)

    def lookups(self):
        return list(self._LOOKUPS)
