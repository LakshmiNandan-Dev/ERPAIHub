"""Thin FastAPI wrapper over QueryService (optional dependency).

    POST /query     {question, schema_id} -> proposed SQL (preview, not executed)
    GET  /schemas   available schema ids
    GET  /health

FastAPI is an optional extra; importing this module without it raises a clear
error. All real work is in `service.py`, which is server-free and tested there.
"""

from .service import QueryService


def build_app(service: QueryService, admin: dict | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse
        from pydantic import BaseModel
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("serving needs fastapi+pydantic: pip install 'tinyllm[serve]'") from e

    from pathlib import Path

    from .jobs import TrainJob
    _STATIC = Path(__file__).resolve().parent / "static"
    _INDEX = _STATIC / "index.html"

    class QueryRequest(BaseModel):
        question: str
        schema_id: str

    class ExecuteRequest(BaseModel):
        schema_id: str
        sql: str                      # the user-confirmed SQL from a prior /query
        max_rows: int = 50

    app = FastAPI(title="TinyLLM NL→Oracle SQL", version="0.1")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _INDEX.read_text(encoding="utf-8")

    @app.get("/health")
    def health():
        return {"status": "ok", "schemas": service.schema_ids()}

    @app.get("/schemas")
    def schemas():
        return {"schemas": service.schema_ids()}

    @app.post("/query")
    def query(req: QueryRequest):
        """Propose SQL for confirmation -- does NOT execute."""
        try:
            return service.query(req.question, req.schema_id).to_dict()
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/execute")
    def execute(req: ExecuteRequest):
        """Run the user-confirmed SELECT read-only against the live DB."""
        try:
            rows = service.execute(req.schema_id, req.sql, max_rows=req.max_rows)
            return {"schema_id": req.schema_id, "row_count": len(rows), "rows": rows}
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    # -- admin / setup console: extract -> fine-tune from the browser --------
    if admin is not None:
        from ..db import SqliteDb
        from ..extract import extract_schema
        from ..schema_graph.serialize import save_schema
        sdir = Path(admin["schemas_dir"]); mdir = Path(admin["models_dir"])
        sdir.mkdir(parents=True, exist_ok=True); mdir.mkdir(parents=True, exist_ok=True)
        job = TrainJob()

        class ExtractRequest(BaseModel):
            schema_id: str
            mock: bool = False
            host: str = ""
            port: int = 1521
            service_name: str = ""
            username: str = ""
            password: str = ""

        class TrainRequest(BaseModel):
            schema_id: str
            steps: int = 300
            n_train: int = 800

        @app.get("/setup", response_class=HTMLResponse)
        def setup():
            return (_STATIC / "setup.html").read_text(encoding="utf-8")

        @app.post("/admin/extract")
        def admin_extract(req: ExtractRequest):
            try:
                if req.mock:
                    schema = extract_schema(mock=True)
                else:
                    dsn = f"{req.username}/{req.password}@{req.host}:{req.port}/{req.service_name}"
                    schema = extract_schema(dsn=dsn)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"extract failed: {e}")
            save_schema(schema, sdir / f"{req.schema_id}.json")
            service.add_schema(req.schema_id, schema, SqliteDb(schema, seed=7))
            return {"schema_id": req.schema_id, "foreign_keys": len(schema.foreign_keys),
                    "tables": [{"name": t.name, "columns": len(t.columns),
                                "flexfields": sum(1 for c in t.columns if c.business_label)}
                               for t in schema.tables]}

        @app.post("/admin/train")
        def admin_train(req: TrainRequest):
            if req.schema_id not in service.schema_ids():
                raise HTTPException(status_code=404, detail="extract this schema first")
            out_dir = str(mdir / req.schema_id)
            try:
                job.start(schema_path=str(sdir / f"{req.schema_id}.json"), schema_id=req.schema_id,
                          init_ckpt=admin["base_ckpt"], init_tok=admin["base_tok"], out_dir=out_dir,
                          steps=req.steps, n_train=req.n_train, log_path=str(mdir / f"{req.schema_id}.log"))
            except RuntimeError as e:
                raise HTTPException(status_code=409, detail=str(e))
            return {"started": True, "schema_id": req.schema_id, "steps": req.steps}

        @app.get("/admin/train/status")
        def admin_train_status():
            st = job.status()
            if st["state"] == "done" and not job.reloaded:
                ck, tk = Path(st["out_dir"]) / "model_best.pt", Path(st["out_dir"]) / "tokenizer.json"
                if ck.exists() and tk.exists():
                    service.reload_model(str(ck), str(tk))
                    job.reloaded = True
            st["reloaded"] = job.reloaded
            return st

    return app
