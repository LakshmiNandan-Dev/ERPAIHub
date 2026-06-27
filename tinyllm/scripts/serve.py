#!/usr/bin/env python3
"""Run the TinyLLM NL->Oracle-SQL service.

    pip install 'fastapi' 'uvicorn'
    python3 scripts/serve.py            # http://127.0.0.1:8000  (POST /query)

Registers two schemas resident in RAM: the extracted (mock) EBS AP/GL catalog
and one synthetic schema. /query proposes SQL for confirmation -- it does not run it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example  # noqa: E402
from tinyllm.db import SqliteDb  # noqa: E402
from tinyllm.extract import EbsExtractor, MockCatalog  # noqa: E402
from tinyllm.serve import QueryService, build_app  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default="artifacts/model_best.pt", help="model to SERVE")
    ap.add_argument("--tok", default="artifacts/tokenizer.json")
    ap.add_argument("--base", default="artifacts/model_best.pt", help="vendor base for admin fine-tunes")
    ap.add_argument("--base-tok", default="artifacts/tokenizer.json")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    schemas = {
        "ebs_ap_gl": EbsExtractor(MockCatalog()).extract(),     # extracted "real" EBS
        "synthetic_demo": generate_example(1_000_000, level=2).schema,
    }
    # SQLite stand-in DBs (synthetic data) so /execute runs end-to-end without
    # Oracle; swap in tinyllm.db.OracleDb(cursor) against a real instance.
    dbs = {sid: SqliteDb(schema, seed=hash(sid) & 0xFFFF) for sid, schema in schemas.items()}
    service = QueryService.from_files(args.ckpt, args.tok, schemas, dbs=dbs)
    print(f"loaded {len(schemas)} schemas: {service.schema_ids()}")

    # admin fine-tunes always start from the VENDOR BASE (not whatever is served)
    admin = {"schemas_dir": "artifacts/customer_schemas",
             "models_dir": "artifacts/customer_models",
             "base_ckpt": args.base, "base_tok": args.base_tok}

    import uvicorn
    uvicorn.run(build_app(service, admin=admin), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
