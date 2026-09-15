# TinyLLM — NL → Oracle SQL for Oracle E-Business Suite

A from-scratch (no pre-trained models) encoder-decoder that translates natural
language into Oracle SQL over the EBS schema. It ships as an on-prem, auditable
**model factory**: a customer runs the whole loop on their own iron —
**extract their catalog → train/fine-tune → serve** — and their schema and data
never leave the network.

> **Status: works end-to-end and transfers to a real EBS catalog.** Trained
> entirely from scratch on synthetic data, the model generalizes across unseen
> synthetic schemas **and** produces correct SQL on an *extracted* EBS catalog,
> which a quick customer-local fine-tune then specializes. The one piece that
> still needs a real instance to exercise is the **live Oracle adapter** (the
> read-only `oracledb` connection); its mapping/SQL are written and the logic is
> mock-tested. See [What's solid / what's left](#whats-solid--whats-left).

## The on-prem workflow (the product)

Two commands, run entirely on the customer's machine:

```bash
# 1. Extract their EBS catalog over a READ-ONLY connection -> schema.json
tinyllm extract --dsn 'readonly_user/pwd@host:1521/EBS' --out schema.json
#   roles, flexfield (segmentN) business labels, lookup domains, and the join
#   graph INFERRED from naming conventions (real EBS declares almost no FKs)
#   PLUS MINED from view/materialized-view/package-body/procedure/function/
#   type-body/trigger SQL text -- joins on a non-*_id business key, or an *_id
#   join whose column name doesn't match the target's PK, are invisible to
#   naming inference alone but stated explicitly in the code that performs them

# 2. Fine-tune the shipped vendor base on THEIR schema (single GPU/CPU, minutes)
tinyllm train --schema schema.json --init base.pt --tok base_tok.json --out customer_model

# 3. Serve: NL question -> proposed SQL -> preview -> confirmed read-only run
tinyllm serve                          # FastAPI + a self-contained web UI at :8000
```

This is **split learning**: the vendor ships an opaque base trained on diverse
synthetic + EBS-realistic schemas; the customer specializes it locally on their
exact tables, lookup values, and flexfield meanings — no customer data ever
leaves, and the richest generation IP stays vendor-side.

## Results (from scratch, ~8M params, dev config d256/8h/4+4L)

**Cross-schema generalization** — trained and evaluated on *disjoint* synthetic
schemas, scored by **execution accuracy** (do predicted and gold SQL return the
same rows?), 80 unseen schemas:

| decode | execution acc | exact | un-runnable |
|---|---:|---:|---:|
| greedy | 0.825 | 0.812 | 12.5% |
| **graph-constrained** | **0.938** | 0.925 | **0.0%** |

**Real-EBS transfer** — the same model run on an *extracted* EBS catalog
(`ap_invoices_all`, `gl_code_combinations`, `vendor_id`, …):

| base | real-EBS execution acc |
|---|---|
| trained on generic/procedural names | 0 / 20 (garbled identifiers) |
| **trained on EBS-realistic names (v4)** | **10 / 20 zero-shot** |
| **+ ~2.5-min customer-local fine-tune** | **0.85 exact-match** on held-out queries |

The lesson that drove the design: real-EBS transfer is a *training-distribution*
problem, not a decoding one — once the synthetic generator emits real EBS naming
(`<module>_<entity>`, `_headers_all`/`_lines_all`, `segmentN`, `vendor_id`-style
keys), the model is in-distribution and transfers. **82 tests pass.**

## How it works

```
schema (synthetic for the base; EXTRACTED for the customer)
  → SchemaGraph        join paths via FK edges (graph owns "form")
  → QuerySampler       AST over an L1–L5 ladder (sampler owns intent)
  → render_oracle      AST → Oracle SQL (ANSI joins, EXTRACT, APPS-synonym targets)
  → render_question    AST → canonical NL + meaning-preserving paraphrases
  → validate           graph (dependency-free) + sqlglot (Oracle dialect)
  → BPE tokenizer      byte-level, from scratch (no external tokenizer libs)
  → encoder-decoder    RMSNorm · RoPE · SwiGLU · 3-way tied embeddings · dropout
  → training           cross-schema (base) or query-level (customer) split
  → retrieval+decode   link relevant tables → graph-constrained beam search
  → execution          translate to SQLite (or live Oracle) → compare result sets
```

EBS shapes are modeled directly: multi-org `_ALL`/`org_id`, flexfield (KFF)
`segmentN` with business labels, lookup-coded columns, and header/lines 2-hop
bridges (the `gl_code_combinations` join). Complexity ladder: **L1** single-table
· **L2** aggregate+GROUP BY · **L3** HAVING / top-N · **L4** nested subquery ·
**L5** window ranking.

### The schema graph is used three times (one source of truth)
- **Generation:** joins sampled by walking real FK edges → correct by construction.
- **Retrieval:** `link_tables` turns a real catalog (hundreds of tables, thousands
  of tokens) into the small training-shaped view the encoder takes — a 165-table
  catalog goes **5,638 → 106 tokens** (limit 512) at **0.95 recall**.
- **Decoding:** `SchemaPrefixGate` prunes, *as the model types*, any token that
  would commit a non-existent table/column or non-FK join key.

The runtime gate chain is the spec's safety design: model proposes → graph
validates form → `EXPLAIN` validates against the live DB → user confirms → a
**read-only** execute. The serving layer never auto-runs SQL.

Model owns intent · graph owns form · execution owns truth.

## Quick start (dev / synthetic)

```bash
pip install -e '.[model,serve]'        # sqlglot, torch, fastapi; add ',oracle' for live extract

python3 scripts/generate_data.py --n 5 --level 2          # inspect generated examples
python3 scripts/generate_data.py --n 5000 --quiet         # throughput + valid-rate
tinyllm generate --n 3 --schema                           # same via the CLI

# train the vendor base on EBS-realistic names (the recipe that transfers)
python3 scripts/train.py --train 4000 --val 300 --steps 3000 \
        --paraphrases 3 --dropout 0.1 --style ebs --device cpu

python3 scripts/exec_eval.py --n 80 --beam 5 --style ebs  # execution accuracy
python3 scripts/extract_demo.py --model                   # extract + run the model on real EBS
python3 scripts/retrieve_demo.py --n 40 --model           # retrieval on a 165-table catalog
tinyllm serve                                             # web UI + REST at :8000
pytest                                                    # full test suite
```

`--style` selects naming: `default` (generic pools) · `procedural` (near-unique,
forces schema-linking) · `ebs` (real EBS conventions — the one that transfers).

> On Apple-Silicon dev boxes use `--device cpu`: for this tiny model CPU is
> faster and far more stable than MPS, which thrashes the unified memory.

## Layout

| Path | Role |
|---|---|
| `tinyllm/schema_graph/` | schema model + `SchemaGraph` + synthetic generator (default/procedural/**ebs**) + JSON `serialize` |
| `tinyllm/sql_sampler/`  | SQL AST/IR + graph-walking sampler (L1–L5) |
| `tinyllm/render/`, `tinyllm/nl/` | AST → Oracle SQL · AST → question (template + paraphrase) |
| `tinyllm/validate/`     | graph (structural) + sqlglot (dialect) validators |
| `tinyllm/tokenizer/`, `tinyllm/model/` | from-scratch byte-level BPE · encoder-decoder |
| `tinyllm/train/`        | cross-schema + customer-local splits, training loop, checkpoints |
| `tinyllm/decode/`       | graph-constrained decoding (incremental gate + optional hard logit-mask) |
| `tinyllm/retrieve/`     | inference-time schema retrieval (question → relevant tables) |
| `tinyllm/extract/`      | EBS catalog → `Schema`: roles, flexfield/lookup meaning, FK inference + code-mined joins (views, materialized views, package bodies, procedures, functions, type bodies, triggers); mock + `oracledb` |
| `tinyllm/eval/`         | execution-accuracy harness (SQLite stand-in DB + result-set compare) |
| `tinyllm/db/`           | runtime DB gate: `EXPLAIN`-validate + read-only execute (SqliteDb / OracleDb) |
| `tinyllm/serve/`        | `QueryService` + FastAPI (`/query`,`/execute`) + self-contained web UI |
| `tinyllm/cli.py`        | `tinyllm` console: `extract` · `train` · `serve` · `query` · `generate` |

Interfaces (`SchemaGraph`, `QuerySampler`, the catalog source, the DB connection)
are clean swap points so native/compiled or real-Oracle implementations drop in
without touching callers — the toolkit ships as auditable source.

## Deployment

The bundled `tinyllm serve` (FastAPI + web UI) is the reference server and runs
the whole gate chain. For other targets:

- **Portable / air-gapped (ONNX):** `scripts/export_onnx.py` exports the encoder
  and decoder-step as two stateless ONNX graphs; ship them + `tokenizer.json` +
  the schema JSONs and run the orchestration loop under `onnxruntime` (no torch).
- **Fleet ops (Triton):** host the two ONNX graphs on Triton's onnxruntime
  backend and the pipeline (retrieve → gated decode → repair) in a Triton
  **Python-backend** orchestrator — see [deploy/triton/](deploy/triton/). Only
  the tensor ops go to Triton; all schema-aware logic is reused from `tinyllm`.
- **Not a fit:** Ollama / vLLM expect *decoder-only* models in GGUF / registered
  HF formats — this is a custom encoder-decoder with a custom tokenizer and a
  graph-constrained decoder, so neither hosts it without reimplementation.

## What's solid / what's left

**Solid (built + tested):** the from-scratch data engine, tokenizer, and model;
cross-schema training; retrieval; incremental graph-constrained decoding;
execution-accuracy eval; the EBS catalog extractor (mapping + FK inference,
plus join-predicate mining from view/materialized-view/package-body/
procedure/function/type-body/trigger text); **real-EBS transfer via
EBS-realistic training**; the customer-local extract→train→serve workflow
with preview-confirm safety.

**Left:**
- **Live Oracle** — the `oracledb` read-only extraction path (`ALL_TABLES`/
  `ALL_TAB_COLUMNS`/`ALL_CONSTRAINTS`/`ALL_INDEXES`/`ALL_VIEWS`/`ALL_MVIEWS`/
  `ALL_SOURCE`/`ALL_TRIGGERS`) has now been run end-to-end against a real
  production EBS instance: 21,880 tables, 20,204 views extracted successfully.
  That run also surfaced (and fixed) a real gap: EBS's own seed tables almost
  never have a PRIMARY KEY constraint — `ap_invoices_all`, `ap_suppliers`,
  `gl_code_combinations` all have zero — only a unique index (Oracle
  Applications' own convention). Without falling back to that, naming-
  convention FK inference finds almost nothing real; `primary_key()` now does.
  The `EXPLAIN`-validate + read-only execute gate is still written and
  mock-tested only, unexercised against a real instance. The PL/SQL join
  miner is a regex-based heuristic (views/mviews get a real sqlglot parse;
  procedural code doesn't) — it only ever adds an edge when both sides
  resolve to a real catalog table, but it can still miss joins expressed
  unconventionally (dynamic SQL, `%TYPE`-driven column names, `wrap`-
  obfuscated PL/SQL).
- **Customer-local training, now fixed for module-scale schemas, still not for
  a full unscoped instance.** `build_pairs_over_schema` used to serialize the
  WHOLE given schema into every example with no retrieval narrowing (fine at
  demo scale; verified broken at real full-EBS scale, ~22K tables — a single
  example's schema text ran ~10MB). Three real bugs are now fixed, verified
  against a real ~700-table EBS module (AP+GL) with a full extract→train
  smoke test that actually completed (loss dropped 4.41→3.62 over 20 steps):
  (1) `link_tables` had no output cap at all — a typical question pulled in
  over half the catalog (~84,000 estimated tokens against a 512-token model);
  it's now bounded by a column-count budget calibrated against the real
  tokenizer (~10 tok/col measured — a char-count guess was off ~8x); (2) even
  correctly retrieved, real (wide) EBS tables — up to 194 columns on one table
  — still didn't fit; `serialize_schema` now accepts a per-table column
  allowlist, and `build_pairs_over_schema` trims each table to the columns the
  gold AST actually references (PK/FK always kept) instead of dumping every
  column; (3) the query sampler crashed outright on a keyless fact table
  (`Table.primary_key` is `None` when a table has no PK — common for EBS
  staging/interface tables — and `_pick_group`'s last-resort fallback didn't
  handle that). Residual: ~19.5% of generated examples still exceed 512 tokens
  (multi-table L4/L5 queries joining several wide real tables) — not a hard
  failure (the model is RoPE-based; `max_seq_len` isn't enforced anywhere in
  `transformer.py`, so longer sequences run, just less efficiently), but a
  known, bounded (max seen: 918 tokens) rather than fully closed gap.
  Making the FULL, unscoped instance directly fine-tunable is still a
  separate, larger follow-up: `link_tables` rebuilds its `SchemaGraph` and
  rescans every table's terms from scratch on every call, fine at module scale
  but not cached for repeated calls against a full ~22K-table catalog.
- **Naming-convention FK inference at full-instance scale** — `pk_owner` in
  `extractor.py` grants a table "ownership" of its PK column NAME globally
  across every owner in scope; at full-instance scale (178 products) generic
  surrogate names (`party_id`, `batch_id`, `task_id`, ...) are each the PK of
  many unrelated tables, so a column can get linked to a semantically
  unrelated table purely by which one the scan happened to see first — e.g.
  `ap_invoices_all.party_id` resolved to an obscure `ar`-owned ETL log table
  instead of the real party master. Scoping extraction to a specific module
  set (e.g. AP+GL) removes most of that ambiguity by construction, since the
  generic names mostly aren't reused within one module. Making the unscoped
  case correct would need real disambiguation (e.g. preferring a same-module
  target, or a confidence signal) rather than first-seen-wins.
- **Last-mile accuracy** — remaining errors are lookup-value / column-selection
  slips (not garbling); the customer fine-tune and value-constrained decoding
  close them.
- **Scale & coverage** — dev config (~8M; vs planned 55–180M); ladder stops at L5
  (no set-ops / correlated subqueries).
- **Packaging** — signed/reproducible artifacts and a third-party security audit
  for the shipped toolkit.

## License

**Proprietary — all rights reserved.** This source is public for evaluation and
reference only; it is **not** open source. Using, running, copying, modifying,
or redistributing it, or using it to build a competing product, requires a
separate written commercial license. See [LICENSE](LICENSE); for licensing
contact palla.nagendra@gmail.com.
