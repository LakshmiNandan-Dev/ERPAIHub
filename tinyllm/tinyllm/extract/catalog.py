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


# -- the real adapter's SQL (documented; run by OracleCatalog) ----------------
# Read-only, returns no business data -- only the data dictionary + FND setup.
ORACLE_SQL = {
    # installed-module tables, resolved through the APPS synonyms layer
    "tables": """
        SELECT s.synonym_name
          FROM all_synonyms s
          JOIN fnd_product_installations i ON i.status = 'I'
         WHERE s.owner = 'APPS' AND s.table_owner = i.oracle_id
    """,
    "columns": """
        SELECT column_name, data_type, nullable
          FROM all_tab_columns WHERE table_name = :t ORDER BY column_id
    """,
    "primary_key": """
        SELECT cc.column_name
          FROM all_constraints c
          JOIN all_cons_columns cc ON cc.constraint_name = c.constraint_name
         WHERE c.constraint_type = 'P' AND c.table_name = :t
    """,
    "foreign_keys": """
        SELECT c.table_name, cc.column_name, rc.table_name, rcc.column_name
          FROM all_constraints c
          JOIN all_cons_columns cc  ON cc.constraint_name  = c.constraint_name
          JOIN all_constraints rc   ON rc.constraint_name  = c.r_constraint_name
          JOIN all_cons_columns rcc ON rcc.constraint_name = rc.constraint_name
         WHERE c.constraint_type = 'R'
    """,
    # flexfield segment -> business meaning (the customer-specific mapping)
    "flex_segments": """
        SELECT t.application_table_name, s.application_column_name, s.segment_name
          FROM fnd_id_flex_segments s
          JOIN fnd_id_flex_segments_tl tl ON tl.id_flex_num = s.id_flex_num
         WHERE s.enabled_flag = 'Y'
    """,
    "lookups": """
        SELECT lookup_type, lookup_code, meaning
          FROM fnd_lookup_values WHERE language = USERENV('LANG')
    """,
}


class OracleCatalog(CatalogSource):
    """Real adapter: runs ORACLE_SQL against a read-only cursor. Untested here
    (no Oracle in the dev env); the mapping logic is tested via MockCatalog."""

    def __init__(self, cursor):
        self.cur = cursor

    def _rows(self, key, **bind):
        self.cur.execute(ORACLE_SQL[key], bind)
        return self.cur.fetchall()

    def tables(self):
        return [r[0].lower() for r in self._rows("tables")]

    def columns(self, table):
        return [RawColumn(r[0].lower(), r[1], r[2] == "Y")
                for r in self._rows("columns", t=table.upper())]

    def primary_key(self, table):
        return [r[0].lower() for r in self._rows("primary_key", t=table.upper())]

    def foreign_keys(self):
        return [RawFk(r[0].lower(), r[1].lower(), r[2].lower(), r[3].lower())
                for r in self._rows("foreign_keys")]


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
