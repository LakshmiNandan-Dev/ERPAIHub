"""End-to-end data-engine slice: schema -> sample -> render -> NL -> validate.

Produces the training triple (serialized_schema, question, gold_sql) plus the
AST and provenance/feature metadata. This is the unit the trainer will consume
and the eval harness will score by execution accuracy.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .nl import llm_paraphrases
from .nl import paraphrases as make_paraphrases
from .nl import render_question
from .render import render_oracle
from .schema_graph import Schema, SchemaGraph, SyntheticSchemaGenerator
from .sql_sampler import QuerySampler, SelectQuery
from .validate import ValidationResult, validate_graph, validate_sqlglot


@dataclass
class Example:
    schema: Schema
    level: int
    question: str
    sql: str
    ast: SelectQuery
    ebs_features: list[str] = field(default_factory=list)
    provenance: str = "synthetic"
    validations: list[ValidationResult] = field(default_factory=list)
    paraphrases: list[str] = field(default_factory=list)

    def training_pairs(self) -> list[tuple[str, str]]:
        """(question, sql) pairs -- the canonical question plus any paraphrases."""
        questions = [self.question] + self.paraphrases
        return [(q, self.sql) for q in dict.fromkeys(questions)]

    @property
    def valid(self) -> bool:
        # SKIP (ok is None) does not count as failure
        return all(v.ok is not False for v in self.validations)


def example_from_schema(schema: Schema, rng: random.Random, level: int = 2,
                        n_paraphrases: int = 0, para_rng: random.Random | None = None) -> Example:
    """Sample one (question, SQL) example over a GIVEN schema -- works for both a
    synthetic schema and a customer's extracted EBS catalog."""
    graph = SchemaGraph(schema)
    ast, features = QuerySampler(graph, rng).sample(level)
    sql = render_oracle(ast)
    question = render_question(ast)
    validations = [validate_graph(ast, graph), validate_sqlglot(sql)]
    phrases: list[str] = []
    if n_paraphrases > 0:
        phrases = make_paraphrases(ast, n_paraphrases, para_rng or random.Random())
        # vendor-side LLM paraphrases stack on top (no-op air-gap default)
        phrases = list(dict.fromkeys(phrases + llm_paraphrases(question, n_paraphrases)))
    return Example(
        schema=schema, level=level, question=question, sql=sql, ast=ast,
        ebs_features=features, validations=validations, paraphrases=phrases,
    )


def generate_example(seed: int, level: int = 2, n_paraphrases: int = 0,
                     style: str = "default", procedural: bool = False) -> Example:
    if procedural:                                   # back-compat alias
        style = "procedural"
    schema = SyntheticSchemaGenerator(seed, style=style).generate()
    return example_from_schema(schema, random.Random(seed), level, n_paraphrases,
                               random.Random(seed ^ 0x9E3779B9))


def serialize_schema(schema: Schema, tables: list[str] | None = None) -> str:
    """Encoder-input serialization: tables : columns(pk/fk/role) | ...

    `tables` lets us emit only the retrieved subgraph; None emits all.
    """
    fk_index = {(fk.from_table, fk.from_column): fk for fk in schema.foreign_keys}
    chunks = []
    for t in schema.tables:
        if tables is not None and t.name not in tables:
            continue
        cols = []
        for c in t.columns:
            tags = []
            if c.is_pk:
                tags.append("pk")
            fk = fk_index.get((t.name, c.name))
            if fk:
                tags.append(f"fk->{fk.to_table}.{fk.to_column}")
            if c.business_label:
                tags.append(f"={c.business_label}")
            suffix = f"({','.join(tags)})" if tags else ""
            cols.append(f"{c.name}{suffix}")
        chunks.append(f"{t.name} : {', '.join(cols)}")
    return " | ".join(chunks)
