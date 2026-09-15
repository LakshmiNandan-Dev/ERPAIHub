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


@dataclass
class RawCodeObject:
    name: str
    kind: str            # "view" | "materialized_view" | "package_body" | "procedure"
                          # | "function" | "type_body" | "trigger"
    text: str


class CatalogSource:
    """Interface every catalog reader implements (real Oracle or mock)."""

    def tables(self) -> list[str]: raise NotImplementedError
    def columns(self, table: str) -> list[RawColumn]: raise NotImplementedError
    def primary_key(self, table: str) -> list[str]: raise NotImplementedError
    def foreign_keys(self) -> list[RawFk]: return []          # EBS often declares none
    def flex_segments(self) -> list[RawFlex]: return []
    def lookups(self) -> list[RawLookup]: return []
    def code_objects(self) -> list[RawCodeObject]: return []  # views + PL/SQL bodies


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
    """The bulk queries, parameterized by the in-scope-owner subquery."""
    return {
        # table LIST from ALL_TABLES (everything real in the in-scope owners)
        "tables": f"""
            SELECT t.owner, t.table_name
              FROM all_tables t
             WHERE t.owner IN ({scope})
               AND t.table_name NOT LIKE '%$%'
        """,
        # APPS + PUBLIC synonym overlay: (owner, table) -> canonical name.
        # PUBLIC matters as much as APPS here -- confirmed against a real
        # instance: APPS-owned code routinely references tables completely
        # unqualified ("FROM AP_INVOICES_ALL", no owner, no alias-worthy
        # synonym-name games), which Oracle can only resolve via a PUBLIC
        # synonym when APPS has no PRIVATE one of its own. Missing PUBLIC
        # meant those tables fell back to owner-qualified names even though
        # the exact bare name real EBS code already uses was resolvable.
        # APPS-owned still wins over PUBLIC when both exist for one table
        # (Oracle's own resolution order: private beats public) -- see the
        # merge in _load() below.
        "synonyms": f"""
            SELECT s.synonym_name, s.table_owner, s.table_name, s.owner
              FROM all_synonyms s
             WHERE s.owner IN ('APPS', 'PUBLIC')
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
        # unique-index columns, keyed by base (owner, table, index): EBS's OWN
        # seed tables overwhelmingly have NO formal PRIMARY KEY constraint --
        # the real key is a unique index instead (Oracle Applications' own
        # convention, historically named <table>_U1, though not reliably so
        # for long/"_ALL" names -- ap_invoices_all's is AP_INVOICES_U1, not
        # AP_INVOICES_ALL_U1). Used as a fallback in _load() only for tables
        # the constraint-based query above found nothing for.
        "unique_indexes": f"""
            SELECT i.owner, i.table_name, i.index_name, ic.column_name, ic.column_position
              FROM all_indexes i
              JOIN all_ind_columns ic
                ON ic.index_owner = i.owner AND ic.index_name = i.index_name
             WHERE i.owner IN ({scope})
               AND i.uniqueness = 'UNIQUE'
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
        # view definitions -- single SELECT text per view, sqlglot-parseable
        "views": f"""
            SELECT v.view_name, v.text
              FROM all_views v
             WHERE v.owner IN ({scope})
        """,
        # materialized view defining queries -- same shape as views (one clean
        # SELECT per object), separate dictionary view from ALL_VIEWS
        "mviews": f"""
            SELECT m.mview_name, m.query
              FROM all_mviews m
             WHERE m.owner IN ({scope})
        """,
        # PL/SQL bodies, stored ONE ROW PER LINE -- concatenated client-side by
        # (name, type) below; TYPE = the code kinds worth mining for embedded
        # joins (package/type SPECs carry no executable SQL, only declarations)
        "source": f"""
            SELECT s.name, s.type, s.line, s.text
              FROM all_source s
             WHERE s.owner IN ({scope})
               AND s.type IN ('PACKAGE BODY', 'PROCEDURE', 'FUNCTION', 'TYPE BODY')
             ORDER BY s.name, s.type, s.line
        """,
        # trigger bodies -- one row per trigger, body text only (no CREATE
        # TRIGGER header/WHEN clause -- fine, we only mine equality predicates)
        "triggers": f"""
            SELECT t.trigger_name, t.trigger_body
              FROM all_triggers t
             WHERE t.owner IN ({scope})
        """,
    }


# the default queries (no extra owners) -- handy for reference/inspection
ORACLE_SQL = _queries(_SCOPE_BASE)


class OracleCatalog(CatalogSource):
    """Real adapter (HYBRID): the table list is read from ALL_TABLES for every
    licensed/shared (+ extra) owner, then renamed to its canonical APPS synonym
    when one exists, else kept owner-qualified. BULK set-based reads, cached once.

    Primary keys fall back to a unique index when no PRIMARY KEY constraint
    exists (`unique_indexes` in `_load()`) -- confirmed against a real EBS
    instance that this is the norm, not the exception: core seed tables like
    `ap_invoices_all`, `ap_suppliers`, and `gl_code_combinations` have ZERO
    'P'-type constraints, only a unique index (Oracle Applications' own
    long-standing convention). Without this fallback, naming-convention FK
    inference in `extractor.py` finds almost nothing real, since `pk_owner`
    requires a table to own its PK column NAME and most tables never register
    one at all.

    Owner-qualified table names ("owner.table") are also the norm, not the
    exception, in practice -- APPS-owned synonyms only covered ~9% of tables
    on the real instance this was verified against (many synonyms there are
    owned by a read-only clone schema like APPSRO instead, or don't exist).
    That's still correct behavior: an owner-qualified reference is guaranteed
    executable by the connected account; a bare name that account can't
    actually resolve unqualified would not be.

    The grouping/renaming/caching logic is tested via a fake cursor in
    test_extract; the live SQL still needs a real read-only EBS account (with
    dictionary access) to confirm against a given instance. Flexfield/lookup
    enrichment for live extraction is a documented follow-up -- the extractor
    degrades gracefully when those are absent.

    `code_objects()` additionally bulk-reads view text (`ALL_VIEWS`),
    materialized view queries (`ALL_MVIEWS`), PL/SQL bodies (`ALL_SOURCE`, one
    row per line -- concatenated here by (name, type): package bodies,
    standalone procedures/functions, and object type bodies) and trigger
    bodies (`ALL_TRIGGERS`); `code_mining.mine_join_hints` scans that text for
    join predicates the naming-convention inference in `extractor.py` can't
    see -- most notably any join on a non-`*_id` business key (e.g. GL's
    `period_name`), which naming inference structurally never considers, plus
    the `*_id` case where the column name doesn't match the target's PK name.
    Same caveat as the rest of this adapter: written and tested via a fake
    cursor, unexercised against a real instance. Two known gaps: `wrap`-
    obfuscated PL/SQL (common on stock Oracle-delivered EBS code) yields no
    text to mine, and code in an owner outside the licensed/shared scope above
    is never read."""

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
        self._code: list[RawCodeObject] | None = None

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
        # 0. the connected user needs no synonym at all for tables IT owns --
        # Oracle resolves an unqualified name in your OWN schema before ever
        # checking a synonym, public or private. Confirmed needed extracting
        # AS the apps account itself: 1,153 of APPS's own tables had no
        # synonym of either kind yet are trivially bare-referenceable, and
        # were falling back to a needless "apps.table_name" qualification.
        self.cur.execute("SELECT USER FROM dual")
        connected_user = self.cur.fetchall()[0][0].upper()
        # 1. synonym overlay first: base (owner, table) -> canonical name.
        # APPS-owned wins over PUBLIC when a table has both (Oracle's own
        # resolution order: a private synonym shadows a public one of the
        # same/different name) -- track which kind won each key so a later
        # PUBLIC row can't overwrite an already-settled APPS one.
        self.cur.execute(self._sql["synonyms"])
        syn: dict[tuple, str] = {}
        syn_is_apps: dict[tuple, bool] = {}
        for name, o, t, syn_owner in self.cur.fetchall():
            key = (o.upper(), t.upper())
            is_apps = syn_owner.upper() == "APPS"
            if key not in syn or (is_apps and not syn_is_apps[key]):
                syn[key] = name.lower()
                syn_is_apps[key] = is_apps
        # 2. table list from ALL_TABLES; canonical = synonym name, else the
        # bare name (if owned by the connected user), else owner.table
        self.cur.execute(self._sql["tables"])
        for owner, tname in self.cur.fetchall():
            if self._skip(tname):
                continue
            key = (owner.upper(), tname.upper())
            fallback = tname.lower() if owner.upper() == connected_user else f"{owner}.{tname}".lower()
            self._canon[key] = syn.get(key, fallback)
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
        # 5. unique-index fallback for tables with NO constraint-based PK above
        # (the EBS norm -- see the "unique_indexes" query comment)
        self.cur.execute(self._sql["unique_indexes"])
        idx_cols: dict[tuple, dict[str, list[tuple[int, str]]]] = {}
        for owner, tname, idx_name, col, pos in self.cur.fetchall():
            key = (owner.upper(), tname.upper())
            idx_cols.setdefault(key, {}).setdefault(idx_name, []).append((pos, col.lower()))
        for key, idxs in idx_cols.items():
            c = self._canon.get(key)
            if not c or self._pk.get(c):
                continue                       # already has a real PK constraint
            # smallest unique index (fewest columns; single-column preferred),
            # tie-broken alphabetically -- this recovers EBS's own "_U1 is the
            # primary key" convention without depending on that exact name
            chosen = min(idxs, key=lambda n: (len(idxs[n]), n))
            self._pk[c] = [col for _, col in sorted(idxs[chosen])]

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

    def code_objects(self):
        if self._code is None:                # views + PL/SQL bodies; cached like the rest
            out: list[RawCodeObject] = []
            self.cur.execute(self._sql["views"])
            for vname, text in self.cur.fetchall():
                if text:
                    out.append(RawCodeObject(vname.lower(), "view", str(text)))

            self.cur.execute(self._sql["mviews"])
            for mname, text in self.cur.fetchall():
                if text:
                    out.append(RawCodeObject(mname.lower(), "materialized_view", str(text)))

            self.cur.execute(self._sql["source"])
            kind_by_type = {
                "PACKAGE BODY": "package_body", "PROCEDURE": "procedure",
                "FUNCTION": "function", "TYPE BODY": "type_body",
            }
            bodies: dict[tuple[str, str], list[str]] = {}
            for oname, otype, _line, text in self.cur.fetchall():
                bodies.setdefault((oname, otype), []).append(text or "")
            for (oname, otype), lines in bodies.items():
                out.append(RawCodeObject(oname.lower(), kind_by_type[otype], "".join(lines)))

            self.cur.execute(self._sql["triggers"])
            for tname, body in self.cur.fetchall():
                if body:
                    out.append(RawCodeObject(tname.lower(), "trigger", str(body)))
            self._code = out
        return list(self._code)


# -- a small AP + GL mock instance (the spec's proposed starter modules) ------
class MockCatalog(CatalogSource):
    """Simulates a tiny EBS: AP invoices/suppliers + the GL accounting flexfield.
    Declares ZERO foreign keys (as real EBS usually does) -- the extractor must
    infer the join graph from naming conventions, PLUS code_objects() mining:
    `ap_invoices_all.terms_id` -> `ap_terms.term_id` (an `*_id` column whose
    name doesn't match the target's PK, mined from a view + a materialized
    view) and `gl_journals_all.ledger_name` -> `gl_ledgers.ledger_name` (a
    non-`*_id` business key naming inference never even considers, mined from
    a TYPE BODY member function)."""

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
            RawColumn("terms_id", "NUMBER"),        # name doesn't match ap_terms.term_id
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
        "ap_terms": [
            RawColumn("term_id", "NUMBER", False),
            RawColumn("name", "VARCHAR2"),
        ],
        "gl_journals_all": [
            RawColumn("journal_id", "NUMBER", False),
            RawColumn("ledger_name", "VARCHAR2"),    # business key, NOT an *_id column
        ],
        "gl_ledgers": [
            RawColumn("ledger_name", "VARCHAR2", False),
            RawColumn("status", "VARCHAR2"),
        ],
    }
    _PK = {
        "ap_suppliers": ["vendor_id"],
        "ap_invoices_all": ["invoice_id"],
        "ap_invoice_lines_all": ["invoice_line_id"],
        "gl_code_combinations": ["code_combination_id"],
        "ap_terms": ["term_id"],
        "gl_journals_all": ["journal_id"],
        "gl_ledgers": ["ledger_name"],
    }
    _CODE = [
        RawCodeObject(
            "ap_invoices_terms_v", "view",
            "SELECT i.invoice_id, t.name terms_name "
            "FROM ap_invoices_all i, ap_terms t "
            "WHERE i.terms_id = t.term_id",
        ),
        RawCodeObject(
            "ap_invoices_terms_mv", "materialized_view",
            "SELECT i.invoice_id, t.name terms_name "
            "FROM ap_invoices_all i, ap_terms t "
            "WHERE i.terms_id = t.term_id",
        ),
        RawCodeObject(
            "gl_ledger_util_ty", "type_body",
            "TYPE BODY gl_ledger_util_ty IS\n"
            "  MEMBER FUNCTION status_for(p_journal_id NUMBER) RETURN VARCHAR2 IS\n"
            "    v_status VARCHAR2(30);\n"
            "  BEGIN\n"
            "    SELECT l.status INTO v_status\n"
            "      FROM gl_journals_all j, gl_ledgers l\n"
            "     WHERE j.journal_id = p_journal_id AND j.ledger_name = l.ledger_name;\n"
            "    RETURN v_status;\n"
            "  END status_for;\n"
            "END;\n",
        ),
    ]
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

    def code_objects(self):
        return list(self._CODE)
