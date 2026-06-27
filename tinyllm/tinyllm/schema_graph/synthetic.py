"""SyntheticSchemaGenerator -- diverse, EBS-shaped synthetic schemas.

Three naming styles (the schema STRUCTURE is identical across all three -- only
the identifiers differ):
  - "default"    : generic EBS-ish nouns from large pools (varied vocabulary).
  - "procedural" : near-unique pronounceable roots -> forces schema-linking, not
                   memorization (train/val share almost no names).
  - "ebs"        : REAL EBS naming conventions -- module prefixes (ap_/gl_/po_),
                   `<module>_<entity>` dims (ap_suppliers), `<module>_<doc>_all`
                   headers (ap_invoices_all), `<module>_<doc>_lines_all` lines,
                   `gl_code_combinations`, and real id columns (vendor_id,
                   invoice_id, code_combination_id, segmentN, org_id). This makes
                   a real extracted EBS catalog IN-DISTRIBUTION for the model.
"""

from __future__ import annotations

import random

from .types import Column, ColumnType, ForeignKey, Schema, SemanticRole, Table

_ENTITIES = [
    "supplier", "vendor", "customer", "party", "employee", "product", "item",
    "account", "project", "location", "organization", "bank", "carrier", "buyer",
    "requester", "department", "asset", "contract", "warehouse", "ledger",
    "currency", "resource", "operator", "payee",
]
_DOCS = [
    "invoice", "order", "voucher", "receipt", "payment", "shipment", "requisition",
    "journal", "transaction", "claim", "settlement", "adjustment", "transfer",
    "remittance", "disbursement", "accrual",
]
_AMOUNTS = [
    "amount", "total", "net_amount", "entered_amount", "line_amount", "gross_amount",
    "tax_amount", "func_amount", "base_amount", "paid_amount", "extended_amount",
    "accounted_amount",
]
_QTYS = ["quantity", "qty", "ordered_qty", "shipped_qty", "received_qty"]
_STATUSES = {
    "APPROVAL_STATUS": ("APPROVED", "PENDING", "REJECTED"),
    "PROCESS_STATUS": ("OPEN", "CLOSED", "ON_HOLD"),
    "PAYMENT_STATUS": ("PAID", "UNPAID", "PARTIAL"),
    "MATCH_STATUS": ("MATCHED", "UNMATCHED", "NEEDS_REVIEW"),
    "POSTING_STATUS": ("POSTED", "UNPOSTED", "ERROR"),
    "WORKFLOW_STATUS": ("STARTED", "COMPLETE", "SUSPENDED"),
}
_SEGMENT_LABELS = [
    "company", "cost center", "account", "department", "region", "project",
    "product", "line of business", "intercompany", "location", "division", "future use",
]
_NAME_COLS = ["{e}_name", "name", "description", "{e}_description", "title", "display_name"]
_DIM2 = ["category", "class", "type", "region", "status_group", "source", "channel"]
_PREFIXES = ["", "", "", "", "xx_", "xxen_", "cust_", "xxgl_"]

# Procedural roots: near-unique pronounceable identifiers (forces schema-linking).
_CONS = "bcdfgklmnprstvz"
_VOWS = "aeiou"

# Real EBS naming. Modules repeat (weighted toward the common ones); dims carry
# their real id column (ap_suppliers' PK is vendor_id, not supplier_id).
_EBS_MODULES = ["ap", "ar", "gl", "po", "inv", "ont", "pa", "fa", "per", "pay",
                "wsh", "xla", "fun", "ce", "ap", "gl", "po", "ar", "inv"]
_EBS_CUSTOM = ["xx", "xxen", "xxap", "xxgl", "cust"]
_EBS_DIMS = [   # (table_root, pk_id, label)
    ("suppliers", "vendor_id", "vendor"), ("vendors", "vendor_id", "vendor"),
    ("customers", "customer_id", "customer"), ("parties", "party_id", "party"),
    ("banks", "bank_id", "bank"), ("employees", "person_id", "employee"),
    ("items", "inventory_item_id", "item"), ("projects", "project_id", "project"),
    ("ledgers", "ledger_id", "ledger"), ("payees", "payee_id", "payee"),
    ("buyers", "buyer_id", "buyer"), ("locations", "location_id", "location"),
    ("organizations", "organization_id", "organization"),
]
_EBS_DOCS = [   # (plural, singular)
    ("invoices", "invoice"), ("orders", "order"), ("payments", "payment"),
    ("receipts", "receipt"), ("requisitions", "requisition"), ("journals", "journal"),
    ("vouchers", "voucher"), ("shipments", "shipment"), ("transactions", "transaction"),
    ("distributions", "distribution"), ("checks", "check"), ("accruals", "accrual"),
]
_EBS_AMOUNTS = [
    "invoice_amount", "amount", "line_amount", "entered_amount", "accounted_amount",
    "tax_amount", "gross_amount", "net_amount", "paid_amount", "base_amount",
    "func_amount", "unpaid_amount",
]


class SyntheticSchemaGenerator:
    def __init__(self, seed: int, style: str = "default"):
        assert style in ("default", "procedural", "ebs"), style
        self.rng = random.Random(seed)
        self.seed = seed
        self.style = style

    def _root(self) -> str:
        rng = self.rng
        syls = []
        for _ in range(rng.randint(2, 3)):
            s = rng.choice(_CONS) + rng.choice(_VOWS)
            if rng.random() < 0.4:
                s += rng.choice(_CONS)
            syls.append(s)
        return "".join(syls)

    def generate(self) -> Schema:
        rng = self.rng
        ebs = self.style == "ebs"

        if ebs:
            module = rng.choice(_EBS_MODULES)
            if rng.random() < 0.2:                       # some custom-extension schemas
                module = rng.choice(_EBS_CUSTOM) + module
            prefix = module + "_"
            entity, entity_pk, ent_label = rng.choice(_EBS_DIMS)
            doc_plural, doc = rng.choice(_EBS_DOCS)
            amount_pool = _EBS_AMOUNTS
        else:
            prefix = rng.choice(_PREFIXES)
            entity = self._root() if self.style == "procedural" else rng.choice(_ENTITIES)
            doc = self._root() if self.style == "procedural" else rng.choice(_DOCS)
            entity_pk, ent_label, doc_plural, amount_pool = f"{entity}_id", entity, doc, _AMOUNTS

        def tn(name: str) -> str:
            return prefix + name

        tables: list[Table] = []
        fks: list[ForeignKey] = []

        # --- dimension: an entity table (1-hop target) ---------------------
        name_col = f"{ent_label}_name" if ebs else rng.choice(_NAME_COLS).format(e=entity)
        tables.append(Table(tn(entity), [
            Column(entity_pk, ColumnType.NUMBER, is_pk=True, role=SemanticRole.ID),
            Column(name_col, ColumnType.VARCHAR2, role=SemanticRole.NAME),
            Column(f"{ent_label}_number", ColumnType.VARCHAR2, role=SemanticRole.CODE),
        ]))

        # --- optional second dimension -------------------------------------
        include_dim2 = rng.random() < 0.5
        d2_tbl = d2_pk = None
        if include_dim2:
            if ebs:
                d2, d2_pk, d2_label = rng.choice([x for x in _EBS_DIMS if x[0] != entity])
            else:
                if self.style == "procedural":
                    d2 = self._root()
                    while d2 == entity:
                        d2 = self._root()
                else:
                    d2 = rng.choice([x for x in _DIM2 if x != entity])
                d2_pk, d2_label = f"{d2}_id", d2
            d2_tbl = tn(d2)
            tables.append(Table(d2_tbl, [
                Column(d2_pk, ColumnType.NUMBER, is_pk=True, role=SemanticRole.ID),
                Column(f"{d2_label}_name", ColumnType.VARCHAR2, role=SemanticRole.NAME),
            ]))

        # --- optional flexfield (KFF) code-combination ---------------------
        include_flex = rng.random() < 0.6
        cc_tbl = None
        if include_flex:
            cc_tbl = "gl_code_combinations" if ebs else tn("code_combinations")
            n_seg = rng.randint(2, 5)
            labels = rng.sample(_SEGMENT_LABELS, n_seg)
            seg = [
                Column(f"segment{i + 1}", ColumnType.VARCHAR2,
                       role=SemanticRole.FLEXFIELD_SEGMENT, business_label=labels[i])
                for i in range(n_seg)
            ]
            tables.append(Table(cc_tbl, [
                Column("code_combination_id", ColumnType.NUMBER, is_pk=True, role=SemanticRole.ID)
            ] + seg))

        # --- header (_ALL multi-org) document table ------------------------
        multi_org = rng.random() < 0.8
        status_type = rng.choice(list(_STATUSES))
        if ebs:
            header = tn(f"{doc_plural}_all" if multi_org else doc_plural)
            status_col = f"{doc}_type_lookup_code"
        else:
            header = tn(f"{doc}_headers_all" if multi_org else f"{doc}_headers")
            status_col = status_type.lower() + "_code"
        header_pk = f"{doc}_id"
        cols = [
            Column(header_pk, ColumnType.NUMBER, is_pk=True, role=SemanticRole.ID),
            Column(entity_pk, ColumnType.NUMBER, role=SemanticRole.ID),
            Column(f"{doc}_date", ColumnType.DATE, role=SemanticRole.DATE),
            Column(rng.choice(amount_pool), ColumnType.NUMBER, role=SemanticRole.AMOUNT),
            Column(status_col, ColumnType.VARCHAR2, role=SemanticRole.LOOKUP,
                   lookup_type=status_type, allowed_values=_STATUSES[status_type]),
        ]
        if ebs:                                          # real EBS audit/header columns
            cols.insert(1, Column(f"{doc}_num", ColumnType.VARCHAR2, role=SemanticRole.CODE))
            cols.append(Column("creation_date", ColumnType.DATE, role=SemanticRole.DATE))
            if rng.random() < 0.5:
                cols.append(Column("payment_status_flag", ColumnType.VARCHAR2,
                                   role=SemanticRole.LOOKUP, lookup_type="PAYMENT_STATUS",
                                   allowed_values=("Y", "N", "P")))
        if include_dim2:
            cols.insert(2, Column(d2_pk, ColumnType.NUMBER, role=SemanticRole.ID))
        if multi_org:
            cols.insert(2, Column("org_id", ColumnType.NUMBER, role=SemanticRole.ORG_ID))
        tables.append(Table(header, cols, is_multi_org=multi_org))
        fks.append(ForeignKey(header, entity_pk, tn(entity), entity_pk))
        if include_dim2:
            fks.append(ForeignKey(header, d2_pk, d2_tbl, d2_pk))

        # --- lines table (enables 2-hop join to the flexfield) -------------
        if include_flex or rng.random() < 0.5:
            lines = tn(f"{doc}_lines_all" if multi_org else f"{doc}_lines")
            lcols = [
                Column(f"{doc}_line_id", ColumnType.NUMBER, is_pk=True, role=SemanticRole.ID),
                Column(header_pk, ColumnType.NUMBER, role=SemanticRole.ID),
                Column(rng.choice(amount_pool), ColumnType.NUMBER, role=SemanticRole.AMOUNT),
            ]
            if rng.random() < 0.5:
                lcols.append(Column(rng.choice(_QTYS), ColumnType.NUMBER, role=SemanticRole.QUANTITY))
            if include_flex:
                lcols.append(Column("code_combination_id", ColumnType.NUMBER, role=SemanticRole.ID))
            tables.append(Table(lines, lcols, is_multi_org=multi_org))
            fks.append(ForeignKey(lines, header_pk, header, header_pk))
            if include_flex:
                fks.append(ForeignKey(lines, "code_combination_id", cc_tbl, "code_combination_id"))

        return Schema(name=f"syn_{doc}_{self.seed}", tables=tables, foreign_keys=fks)
