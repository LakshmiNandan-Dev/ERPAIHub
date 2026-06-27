# Serving TinyLLM on NVIDIA Triton (sketch)

The key idea: **only the two stateless tensor ops go to ONNX/Triton.** The
autoregressive loop, the graph-constraint masking, retrieval, and schema-repair —
the parts that make a tiny model usable — stay as *reused Python* in a Triton
**Python-backend** orchestrator. Triton just gives you the standard serving
surface (HTTP/gRPC, batching, metrics, GPU placement) without re-implementing any
of the schema logic.

```
client {question, schema_id}
        │
        ▼
  tinyllm  (python backend) ── orchestrates ───────────────┐
   • tokenize (question + retrieved schema)                 │  BLS calls
   • link_tables(question, schema)        [tinyllm.retrieve]│  (pb_utils.InferenceRequest)
   • encode  ───────────────────────────────────────────►  encoder  (onnxruntime backend)
   • loop: decoder_step under SchemaPrefixGate masking ──►  decoder_step (onnxruntime backend)
   • schema_repair(sql, question, schema) [tinyllm.decode] │
   • return proposed SQL + gate flags  ◄────────────────────┘
```

`EXPLAIN`-validate and the read-only execute stay **outside** Triton (they're DB
calls against the customer's Oracle) — the client or a thin sidecar runs them
after the user confirms the previewed SQL.

## Model repository

```
deploy/triton/models/
  encoder/                 # onnxruntime backend
    config.pbtxt
    1/model.onnx           # <- scripts/export_onnx.py
  decoder_step/            # onnxruntime backend
    config.pbtxt
    1/model.onnx
  tinyllm/                 # python backend (the orchestrator)
    config.pbtxt
    1/model.py             # <- deploy/triton/model.py here
    1/tokenizer.json       # the from-scratch BPE
    1/schemas/*.json       # extracted catalogs (schema_graph.serialize)
```

## Build & run

```bash
pip install '.[model,onnx]'
python3 scripts/export_onnx.py --out deploy/triton/models/encoder/1   # writes encoder.onnx
# (split the two graphs into encoder/1/model.onnx and decoder_step/1/model.onnx)

# the python backend needs tinyllm importable inside the Triton container:
#   pip install . onnxruntime   (in the tritonserver image)
tritonserver --model-repository=deploy/triton/models

curl -s localhost:8000/v2/models/tinyllm/infer -d '{
  "inputs":[{"name":"question","datatype":"BYTES","shape":[1],"data":["total invoice amount by cost center"]},
            {"name":"schema_id","datatype":"BYTES","shape":[1],"data":["ebs_ap_gl"]}]}'
```

## config.pbtxt (encoder — decoder_step is analogous)

```
name: "encoder"
backend: "onnxruntime"
max_batch_size: 8
input  [ { name: "src" datatype: TYPE_INT64 dims: [-1] },
         { name: "src_keep" datatype: TYPE_BOOL dims: [-1] } ]
output [ { name: "memory" datatype: TYPE_FP32 dims: [-1, 256] } ]
instance_group [ { kind: KIND_GPU } ]   # KIND_CPU is plenty for ~8M params
```

```
name: "tinyllm"          # python backend orchestrator
backend: "python"
max_batch_size: 0
input  [ { name: "question" datatype: TYPE_STRING dims: [1] },
         { name: "schema_id" datatype: TYPE_STRING dims: [1] } ]
output [ { name: "sql" datatype: TYPE_STRING dims: [1] },
         { name: "graph_valid" datatype: TYPE_BOOL dims: [1] } ]
```

## When is this worth it?

For an 8M model the `tinyllm serve` FastAPI server is already fine. Reach for
Triton when you want **fleet-standard ops** (shared metrics/auth/autoscaling
across many models), GPU bin-packing, or to co-host TinyLLM beside other models.
It does **not** make a tiny model meaningfully faster — its value is operational
standardization, not throughput at this size.

## ONNX-only (no Triton)

`scripts/export_onnx.py` alone gives a torch-free, portable artifact: ship
`encoder.onnx` + `decoder_step.onnx` + `tokenizer.json` + the schema JSONs, and
run the same orchestrator (`deploy/triton/model.py`'s logic) under plain
`onnxruntime` — ideal for a minimal, auditable air-gapped appliance.
