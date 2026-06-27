# TinyLLM as a containerized entity in the ERP AI Hub

Package TinyLLM as its **own service** (like Ollama) and drop it into the hub.
The hub keeps its general LLM (Ollama); TinyLLM is the **grounded NL → Oracle EBS
SQL** specialist the hub calls for data questions.

## Build & run

```bash
# from the repo root (the build bakes artifacts/model_best.pt + tokenizer.json)
docker build -t tinyllm:latest -f deploy/docker/Dockerfile .
docker run -d --name tinyllm -p 8000:8000 -v tinyllm_data:/data tinyllm:latest
# or the whole hub side-by-side:
docker compose -f deploy/docker/docker-compose.yml up -d
```

Exposes on `:8000` — `POST /query`, `POST /execute`, `GET /schemas`, and the
`/setup` admin console (extract + fine-tune). `/data` (a volume) persists the
customer's extracted schemas and local fine-tunes across restarts.

## Wire it into the hub

The hub's LLM calls TinyLLM as a tool/microservice (it never runs *inside*
Ollama — different architecture):

```bash
curl -X POST http://tinyllm:8000/query \
  -d '{"question":"total payables by cost center for OU 101","schema_id":"ebs_prod"}'
# -> { sql, graph_valid, explain_ok, tables_used, ... }   (preview; not executed)
```

Register that call as a tool (`ebs_nl_to_sql`) the hub's LLM invokes for data
questions; the hub then previews the SQL, runs it read-only (`/execute` or
against your Oracle), and the LLM narrates the rows.

## Specialize on the customer instance

Open `http://<host>:8000/setup` → enter a **read-only** EBS DSN → **Extract** →
**Fine-tune**. The model hot-reloads; results land in `/data`. Nothing leaves
the network. (Or scripted: `docker exec tinyllm tinyllm extract --dsn ... &&
tinyllm train --schema ...`.)

## Notes

- **Size / slim variant:** this image bundles CPU torch (~1–1.5 GB). For a
  torch-free, much smaller image, export to ONNX (`scripts/export_onnx.py`) and
  serve via onnxruntime / Triton (see [../triton/](../triton/)).
- **Security:** use a read-only, least-privilege Oracle account; `EXPLAIN`
  returns no rows. The image contains the vendor base **weights (IP)** — push it
  only to a **private** registry, never a public one.
- **GPU:** unnecessary at this size; CPU is fine. (`tinyllm serve --device cuda`
  if you co-locate on a GPU node.)
