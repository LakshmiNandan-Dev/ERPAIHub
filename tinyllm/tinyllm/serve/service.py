"""QueryService -- the product inference path, framework-agnostic.

One call ties the whole runtime together:

    question + schema_id
      -> retrieve   (link_tables: which tables does the question touch?)
      -> serialize  (only the retrieved subgraph -> fits the encoder)
      -> decode     (picard_generate: graph-constrained, can't hallucinate ids)
      -> verify     (graph_check_sql: structural gate)
      -> PREVIEW    (propose SQL for confirmation; we do NOT execute)

Preview-not-execute is deliberate: per the design, the model proposes, the graph
gives form, and *execution owns truth* only after the user confirms intent
against a read-only connection. The web layer (`app.py`) is a thin wrapper; all
logic lives here so it is testable without a server.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from ..db import DbConnection
from ..decode import graph_check_sql, picard_generate, schema_repair
from ..model import collate
from ..pipeline import serialize_schema
from ..retrieve import link_tables
from ..schema_graph import SchemaGraph
from ..schema_graph.types import Schema


@dataclass
class QueryResult:
    schema_id: str
    question: str
    sql: str
    tables_used: list[str]
    graph_valid: bool                   # passes the structural gate against the schema
    constrained: bool                   # True = a fully graph-valid decode (not a fallback)
    explain_ok: Optional[bool] = None   # live-DB EXPLAIN gate (None if no DB wired in)
    requires_confirmation: bool = True   # preview-confirm before any execution
    executed: bool = False               # /query never runs the SQL; /execute does
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class QueryService:
    def __init__(self, model, tok, schemas: dict[str, Schema],
                 dbs: Optional[dict[str, DbConnection]] = None,
                 beam: int = 5, max_len: int = 120, device: str = "cpu"):
        self.model = model
        self.tok = tok
        self.schemas = schemas
        self.dbs = dbs or {}
        self.graphs = {sid: SchemaGraph(s) for sid, s in schemas.items()}
        self.beam, self.max_len, self.device = beam, max_len, device

    @classmethod
    def from_files(cls, ckpt: str, tok_path: str, schemas: dict[str, Schema], **kw):
        from ..tokenizer import BPETokenizer
        from ..train import load_model
        device = kw.get("device", "cpu")
        return cls(load_model(ckpt, device=device), BPETokenizer.load(tok_path), schemas, **kw)

    def schema_ids(self) -> list[str]:
        return list(self.schemas)

    def add_schema(self, schema_id: str, schema: Schema, db: Optional[DbConnection] = None):
        """Register a newly-extracted schema at runtime (admin extract)."""
        self.schemas[schema_id] = schema
        self.graphs[schema_id] = SchemaGraph(schema)
        if db is not None:
            self.dbs[schema_id] = db

    def reload_model(self, ckpt: str, tok_path: str):
        """Hot-swap to a freshly fine-tuned checkpoint (admin train completion)."""
        from ..tokenizer import BPETokenizer
        from ..train import load_model
        self.model = load_model(ckpt, device=self.device)
        self.tok = BPETokenizer.load(tok_path)

    def _check(self, schema_id: str):
        if schema_id not in self.schemas:
            raise KeyError(f"unknown schema_id {schema_id!r}; have {self.schema_ids()}")

    def query(self, question: str, schema_id: str) -> QueryResult:
        """Propose SQL (retrieve -> decode -> graph gate -> optional EXPLAIN).
        Never executes -- the caller confirms, then calls execute()."""
        self._check(schema_id)
        schema = self.schemas[schema_id]

        tables = link_tables(question, schema)                       # retrieve
        src_str = serialize_schema(schema, tables=tables)            # serialize subgraph
        batch = collate([(question, src_str, "")], self.tok, self.device)
        sql, constrained = picard_generate(                          # constrained decode
            self.model, self.tok, batch["src"], batch["src_keep"],
            schema, beam=self.beam, max_len=self.max_len,
        )
        sql = schema_repair(sql, question, schema)                   # fix semantic slips
        valid = graph_check_sql(sql, self.graphs[schema_id]).ok is True

        explain_ok = None
        notes = []
        if not (valid and constrained):
            notes.append("low confidence: no fully schema-valid decode found")
        db = self.dbs.get(schema_id)
        if db is not None:                                           # gate (2): live EXPLAIN
            gr = db.explain(sql)
            explain_ok = gr.ok
            if not gr.ok:
                notes.append(f"EXPLAIN rejected by the database: {gr.error}")
        return QueryResult(
            schema_id=schema_id, question=question, sql=sql, tables_used=tables,
            graph_valid=valid, constrained=constrained, explain_ok=explain_ok,
            note="; ".join(notes),
        )

    def execute(self, schema_id: str, sql: str, max_rows: int = 50) -> list:
        """The CONFIRMED step: run a SELECT read-only against the live DB."""
        self._check(schema_id)
        db = self.dbs.get(schema_id)
        if db is None:
            raise RuntimeError(f"no database connected for schema {schema_id!r}")
        return db.run_readonly(sql, max_rows=max_rows)
