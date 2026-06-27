"""Persist an extracted Schema to / from JSON.

The customer extracts their EBS catalog once (read-only), saves it here, inspects
it, and feeds it to local training -- all on their machine. The JSON is the only
thing that moves between extract and train; it never leaves the customer network.
"""

from __future__ import annotations

import json
from pathlib import Path

from .types import Column, ColumnType, ForeignKey, Schema, SemanticRole, Table


def schema_to_dict(schema: Schema) -> dict:
    return {
        "name": schema.name,
        "tables": [
            {
                "name": t.name,
                "is_multi_org": t.is_multi_org,
                "columns": [
                    {
                        "name": c.name,
                        "type": c.type.value,
                        "nullable": c.nullable,
                        "is_pk": c.is_pk,
                        "role": c.role.value if c.role else None,
                        "business_label": c.business_label,
                        "lookup_type": c.lookup_type,
                        "allowed_values": list(c.allowed_values),
                    }
                    for c in t.columns
                ],
            }
            for t in schema.tables
        ],
        "foreign_keys": [
            {"from_table": fk.from_table, "from_column": fk.from_column,
             "to_table": fk.to_table, "to_column": fk.to_column}
            for fk in schema.foreign_keys
        ],
    }


def schema_from_dict(d: dict) -> Schema:
    tables = []
    for t in d["tables"]:
        cols = [
            Column(
                name=c["name"],
                type=ColumnType(c["type"]),
                nullable=c.get("nullable", True),
                is_pk=c.get("is_pk", False),
                role=SemanticRole(c["role"]) if c.get("role") else None,
                business_label=c.get("business_label"),
                lookup_type=c.get("lookup_type"),
                allowed_values=tuple(c.get("allowed_values", ())),
            )
            for c in t["columns"]
        ]
        tables.append(Table(t["name"], cols, is_multi_org=t.get("is_multi_org", False)))
    fks = [ForeignKey(f["from_table"], f["from_column"], f["to_table"], f["to_column"])
           for f in d.get("foreign_keys", [])]
    return Schema(name=d.get("name", "ebs"), tables=tables, foreign_keys=fks)


def save_schema(schema: Schema, path: str | Path) -> None:
    Path(path).write_text(json.dumps(schema_to_dict(schema), indent=2))


def load_schema(path: str | Path) -> Schema:
    return schema_from_dict(json.loads(Path(path).read_text()))
