"""EBS catalog -> our `Schema` (the inference that makes it usable).

The raw data dictionary is just names and types. This turns it into the
semantic, join-aware schema the rest of TinyLLM needs:

  - **semantic roles** from EBS naming/type conventions (`*_id` → ID,
    `*_amount` → AMOUNT, `org_id` → ORG_ID, `segmentN` → flexfield, …);
  - **flexfield meaning** (`segment2` → "cost center") from FND setup;
  - **lookup domains** (coded columns → their allowed values);
  - **the join graph INFERRED from naming conventions** — EBS rarely declares
    DB-level FKs, so we connect `vendor_id`/`code_combination_id`/… to the table
    whose primary key carries that name (plus a small hint table for the cases
    where the column name differs from the target PK).

Same `Schema`/`SchemaGraph` the synthetic generator produces, so everything
downstream (sampler, retrieval, validators, model) consumes it unchanged.
"""

from __future__ import annotations

from ..schema_graph.types import Column, ColumnType, ForeignKey, Schema, SemanticRole, Table
from .catalog import CatalogSource

_AMOUNT_HINTS = ("amount", "total", "price", "cost", "balance", "net", "gross", "tax")
_QTY_HINTS = ("quantity", "qty")
_NAME_SUFFIXES = ("_name", "_description", "description", "title", "display_name")
_CODE_SUFFIXES = ("_number", "_code", "_num")
# EBS FKs whose column name != the target table's PK name (PK-name match misses these)
_FK_HINTS = {
    "set_of_books_id": ("gl_ledgers", "ledger_id"),
    "ledger_id": ("gl_ledgers", "ledger_id"),
}


def _ora_type(t: str) -> ColumnType:
    t = t.upper()
    if t in ("NUMBER", "INTEGER", "FLOAT", "DECIMAL"):
        return ColumnType.NUMBER
    if t.startswith("DATE") or t.startswith("TIMESTAMP"):
        return ColumnType.DATE
    return ColumnType.VARCHAR2


class EbsExtractor:
    def __init__(self, source: CatalogSource):
        self.source = source

    def extract(self) -> Schema:
        src = self.source
        names = src.tables()
        pks = {t: set(src.primary_key(t)) for t in names}
        flex = {(f.table, f.column): f.business_label for f in src.flex_segments()}
        lookups = {(lk.table, lk.column): lk for lk in src.lookups()}
        # which table OWNS each PK column name -> used to infer FK targets
        pk_owner: dict[str, str] = {}
        for t in names:
            for c in pks[t]:
                pk_owner.setdefault(c, t)

        tables: list[Table] = []
        for tname in names:
            cols = [self._column(tname, rc, pks[tname], flex, lookups)
                    for rc in src.columns(tname)]
            multi_org = tname.endswith("_all") and any(c.name == "org_id" for c in cols)
            tables.append(Table(tname, cols, is_multi_org=multi_org))

        return Schema(name="ebs", tables=tables,
                      foreign_keys=self._foreign_keys(src, names, pks, pk_owner))

    def _column(self, tname, rc, pk_set, flex, lookups) -> Column:
        name = rc.name.lower()
        ctype = _ora_type(rc.data_type)
        is_pk = name in pk_set
        role = business_label = lookup_type = None
        values: tuple = ()

        if (tname, name) in flex:
            role, business_label = SemanticRole.FLEXFIELD_SEGMENT, flex[(tname, name)]
        elif (tname, name) in lookups:
            lk = lookups[(tname, name)]
            role, lookup_type, values = SemanticRole.LOOKUP, lk.lookup_type, tuple(lk.values)
        elif name == "org_id":
            role = SemanticRole.ORG_ID
        elif is_pk or name.endswith("_id"):
            role = SemanticRole.ID
        elif ctype == ColumnType.DATE:
            role = SemanticRole.DATE
        elif ctype == ColumnType.NUMBER and any(h in name for h in _QTY_HINTS):
            role = SemanticRole.QUANTITY
        elif ctype == ColumnType.NUMBER and any(h in name for h in _AMOUNT_HINTS):
            role = SemanticRole.AMOUNT
        elif name == "name" or name.endswith(_NAME_SUFFIXES):
            role = SemanticRole.NAME
        elif name.endswith(_CODE_SUFFIXES):
            role = SemanticRole.CODE

        return Column(name, ctype, nullable=rc.nullable, is_pk=is_pk, role=role,
                      business_label=business_label, lookup_type=lookup_type,
                      allowed_values=values)

    def _foreign_keys(self, src, names, pks, pk_owner) -> list[ForeignKey]:
        fks: list[ForeignKey] = []
        seen: set[tuple] = set()

        def add(ft, fc, tt, tc):
            key = (ft, fc, tt, tc)
            if ft != tt and tt in names and key not in seen:
                seen.add(key)
                fks.append(ForeignKey(ft, fc, tt, tc))

        for r in src.foreign_keys():                       # declared (rare in EBS)
            add(r.from_table, r.from_column, r.to_table, r.to_column)

        for t in names:                                    # inferred by convention
            for rc in src.columns(t):
                name = rc.name.lower()
                if name == "org_id" or not name.endswith("_id") or name in pks[t]:
                    continue
                if name in _FK_HINTS:
                    add(t, name, *_FK_HINTS[name])
                elif name in pk_owner and pk_owner[name] != t:
                    add(t, name, pk_owner[name], name)
        return fks
