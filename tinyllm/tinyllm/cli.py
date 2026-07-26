"""`tinyllm` console entry point (installed via `pip install tinyllm`).

    tinyllm serve                       # run the NL->SQL service + web UI
    tinyllm query "top 5 vendors by amount" --schema ebs_ap_gl
    tinyllm generate --n 5 --level 2    # inspect synthetic training examples

The default schema registry is a demo (extracted mock EBS + one synthetic
schema) with SQLite stand-in databases. In a real deployment you would build the
registry from `EbsExtractor(OracleCatalog(cursor))` and wire `OracleDb(cursor)`.
"""

from __future__ import annotations

import argparse


def _demo_registry(with_dbs: bool = True):
    from . import generate_example
    from .extract import EbsExtractor, MockCatalog
    schemas = {
        "ebs_ap_gl": EbsExtractor(MockCatalog()).extract(),
        "synthetic_demo": generate_example(1_000_000, level=2).schema,
    }
    dbs = None
    if with_dbs:
        from .db import SqliteDb
        dbs = {sid: SqliteDb(s, seed=7) for sid, s in schemas.items()}
    return schemas, dbs


def _serve(args):
    import os

    from .serve import QueryService, build_app
    schemas, dbs = _demo_registry()
    service = QueryService.from_files(args.ckpt, args.tok, schemas, dbs=dbs)
    # admin console (extract + fine-tune) writes to a persistable data dir
    admin = {"schemas_dir": os.path.join(args.data, "schemas"),
             "models_dir": os.path.join(args.data, "models"),
             "base_ckpt": args.ckpt, "base_tok": args.tok}
    print(f"serving {service.schema_ids()} on http://{args.host}:{args.port}  (setup at /setup)")
    import uvicorn
    uvicorn.run(build_app(service, admin=admin), host=args.host, port=args.port)


def _query(args):
    from .serve import QueryService
    schemas, dbs = _demo_registry(with_dbs=not args.no_execute)
    service = QueryService.from_files(args.ckpt, args.tok, schemas, dbs=dbs)
    res = service.query(args.question, args.schema)
    print(res.sql)
    print(f"# graph_valid={res.graph_valid} explain_ok={res.explain_ok} "
          f"tables={res.tables_used}")
    if not args.no_execute and res.graph_valid and res.explain_ok:
        rows = service.execute(args.schema, res.sql, max_rows=args.max_rows)
        print(f"# {len(rows)} row(s):")
        for r in rows:
            print("  ", r)


def _generate(args):
    from . import generate_example, serialize_schema
    for i in range(args.n):
        ex = generate_example(args.seed + i, level=args.level)
        print(f"\nQ: {ex.question}\nSQL: {' '.join(ex.sql.split())}")
        if args.schema:
            print(f"schema: {serialize_schema(ex.schema, ex.ast.tables)}")


def _extract(args):
    """Read the customer's EBS catalog (read-only) and save it for local training."""
    from .extract import extract_schema
    from .schema_graph.serialize import save_schema
    schema = extract_schema(dsn=args.dsn, mock=args.mock, extra_owners=args.owner)
    save_schema(schema, args.out)
    print(f"extracted {len(schema.tables)} tables, {len(schema.foreign_keys)} foreign keys "
          f"(inferred where EBS declares none) -> {args.out}")
    for t in schema.tables:
        flex = sum(1 for c in t.columns if c.business_label)
        print(f"  {t.name:32s} {len(t.columns):2d} cols"
              + (f"  ({flex} flexfield segments)" if flex else ""))


def _train(args):
    """Customer-local training: generate data over the EXTRACTED schema(s) and
    train from scratch, or fine-tune a shipped vendor base (--init)."""
    import torch

    from .model import EncoderDecoder, ModelConfig
    from .schema_graph.serialize import load_schema
    from .tokenizer import BPETokenizer
    from .train import TrainConfig, Trainer, corpus_texts, load_model, make_local_split

    device = args.device or ("cpu")
    schemas = [load_schema(p) for p in args.schema]
    print(f"loaded {len(schemas)} schema(s); generating local training data ...")
    train_pairs, val_pairs = make_local_split(schemas, args.train, args.val,
                                              paraphrases=args.paraphrases)
    print(f"  {len(train_pairs):,} train / {len(val_pairs)} val query pairs")

    if args.init:                                    # fine-tune the shipped base
        tok = BPETokenizer.load(args.tok)
        model = load_model(args.init, device=device)
        print(f"  fine-tuning base {args.init} (vocab {tok.vocab_size})")
    else:                                            # train from scratch (local-only mode)
        tok = BPETokenizer().train(corpus_texts(train_pairs), vocab_size=args.vocab)
        cfg = ModelConfig(vocab_size=tok.vocab_size, d_model=args.d_model, n_heads=8,
                          n_enc_layers=4, n_dec_layers=4, dropout=args.dropout,
                          pad_id=tok.special("<pad>"))
        model = EncoderDecoder(cfg)
        print(f"  from scratch: {model.num_params()/1e6:.1f}M params, vocab {tok.vocab_size}")

    tcfg = TrainConfig(total_steps=args.steps, batch_size=args.batch_size, lr=args.lr,
                       eval_every=args.eval_every, warmup=args.warmup,
                       device=device, ckpt_dir=args.out)
    Trainer(model, tok, train_pairs, val_pairs, tcfg).train()
    print(f"customer model -> {args.out}/model_best.pt")


def main(argv=None):
    p = argparse.ArgumentParser(prog="tinyllm", description="NL -> Oracle EBS SQL toolkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the service + web UI + setup console")
    s.add_argument("--ckpt", default="artifacts/model_best.pt")
    s.add_argument("--tok", default="artifacts/tokenizer.json")
    s.add_argument("--data", default="artifacts", help="persistable dir for extracted schemas + customer models")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=_serve)

    q = sub.add_parser("query", help="one-shot NL -> SQL")
    q.add_argument("question")
    q.add_argument("--schema", default="ebs_ap_gl")
    q.add_argument("--ckpt", default="artifacts/model_best.pt")
    q.add_argument("--tok", default="artifacts/tokenizer.json")
    q.add_argument("--no-execute", action="store_true", help="propose only; do not run")
    q.add_argument("--max-rows", type=int, default=20)
    q.set_defaults(func=_query)

    g = sub.add_parser("generate", help="inspect synthetic examples")
    g.add_argument("--n", type=int, default=5)
    g.add_argument("--level", type=int, default=2)
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--schema", action="store_true", help="also print the serialized schema")
    g.set_defaults(func=_generate)

    e = sub.add_parser("extract", help="extract the EBS catalog (read-only) -> schema JSON")
    e.add_argument("--dsn", help="oracledb DSN, e.g. user/pwd@host:1521/EBS (read-only account)")
    e.add_argument("--mock", action="store_true", help="use the built-in AP/GL mock (no Oracle)")
    e.add_argument("--owner", action="append", default=[],
                   help="extra schema owner to include beyond licensed/shared (e.g. a custom XX schema); repeatable")
    e.add_argument("--out", default="schema.json")
    e.set_defaults(func=_extract)

    t = sub.add_parser("train", help="train/fine-tune on the customer's extracted schema(s)")
    t.add_argument("--schema", nargs="+", required=True, help="schema JSON file(s) from extract")
    t.add_argument("--init", help="vendor base checkpoint to fine-tune (omit = from scratch)")
    t.add_argument("--tok", default="artifacts/tokenizer.json", help="base tokenizer (with --init)")
    t.add_argument("--out", default="customer_model")
    t.add_argument("--train", type=int, default=2000)
    t.add_argument("--val", type=int, default=200)
    t.add_argument("--steps", type=int, default=800)
    t.add_argument("--paraphrases", type=int, default=2)
    t.add_argument("--batch-size", type=int, default=48)
    t.add_argument("--lr", type=float, default=5e-4)
    t.add_argument("--vocab", type=int, default=2048)
    t.add_argument("--d-model", type=int, default=256)
    t.add_argument("--dropout", type=float, default=0.1)
    t.add_argument("--eval-every", type=int, default=200)
    t.add_argument("--warmup", type=int, default=50)
    t.add_argument("--device", default="cpu")
    t.set_defaults(func=_train)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
