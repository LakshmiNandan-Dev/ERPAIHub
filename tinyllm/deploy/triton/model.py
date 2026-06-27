"""Triton Python-backend orchestrator for TinyLLM (sketch / reference).

Place at `deploy/triton/models/tinyllm/1/model.py`. It runs the FULL pipeline,
delegating only the two tensor ops to ONNX: everything schema-aware
(retrieval, the decode gate, schema-repair, the graph check) is reused straight
from the `tinyllm` package — no logic is duplicated or lost in translation.

The encoder/decoder are exercised here via in-process onnxruntime for clarity;
in production prefer Triton **BLS** (`pb_utils.InferenceRequest`) so Triton
schedules them as their own (GPU) models.
"""

import os

import numpy as np
import onnxruntime as ort
import triton_python_backend_utils as pb_utils  # provided by the Triton python backend

from tinyllm import serialize_schema
from tinyllm.decode import SchemaPrefixGate, build_token_strings, graph_check_sql, schema_repair
from tinyllm.model.collate import encode_pair
from tinyllm.retrieve import link_tables
from tinyllm.schema_graph import SchemaGraph
from tinyllm.schema_graph.serialize import load_schema
from tinyllm.tokenizer import BPETokenizer

_NEG = -1e9


class TritonPythonModel:
    def initialize(self, args):
        here = os.path.join(args["model_repository"], args["model_version"])
        repo = os.path.dirname(args["model_repository"])
        self.tok = BPETokenizer.load(os.path.join(here, "tokenizer.json"))
        self.tok_strings = build_token_strings(self.tok)
        self.bos, self.eos = self.tok.special("<bos>"), self.tok.special("<eos>")

        sdir = os.path.join(here, "schemas")
        self.schemas = {f[:-5]: load_schema(os.path.join(sdir, f))
                        for f in os.listdir(sdir) if f.endswith(".json")}
        self.gates = {k: SchemaPrefixGate(s) for k, s in self.schemas.items()}
        self.graphs = {k: SchemaGraph(s) for k, s in self.schemas.items()}

        self.enc = ort.InferenceSession(os.path.join(repo, "encoder", "1", "model.onnx"))
        self.dec = ort.InferenceSession(os.path.join(repo, "decoder_step", "1", "model.onnx"))

    def _propose(self, question, schema_id, max_len=120):
        schema, gate = self.schemas[schema_id], self.gates[schema_id]
        tables = link_tables(question, schema)                       # retrieve
        src, _, _ = encode_pair(self.tok, question, serialize_schema(schema, tables), "")
        src = np.array([src], dtype=np.int64)
        src_keep = np.ones_like(src, dtype=bool)
        memory = self.enc.run(None, {"src": src, "src_keep": src_keep})[0]

        toks = [self.bos]                                            # gated greedy (beam plugs in the same way)
        for _ in range(max_len):
            logits = self.dec.run(None, {"tgt": np.array([toks], dtype=np.int64),
                                         "memory": memory, "src_keep": src_keep})[0][0]
            allowed = gate.allowed_next_tokens(self.tok.decode(toks[1:]), self.tok_strings)
            if allowed:                                              # mask to schema-valid ids
                masked = np.full_like(logits, _NEG)
                idx = np.fromiter(allowed, dtype=np.int64)
                masked[idx] = logits[idx]
                logits = masked
            nxt = int(logits.argmax())
            if nxt == self.eos:
                break
            toks.append(nxt)

        sql = schema_repair(self.tok.decode(toks[1:]), question, schema)   # fix semantic slips
        return sql, graph_check_sql(sql, self.graphs[schema_id]).ok is True

    def execute(self, requests):
        out = []
        for req in requests:
            q = pb_utils.get_input_tensor_by_name(req, "question").as_numpy()[0].decode()
            sid = pb_utils.get_input_tensor_by_name(req, "schema_id").as_numpy()[0].decode()
            sql, ok = self._propose(q, sid)
            out.append(pb_utils.InferenceResponse(output_tensors=[
                pb_utils.Tensor("sql", np.array([sql.encode()], dtype=object)),
                pb_utils.Tensor("graph_valid", np.array([ok], dtype=bool)),
            ]))
        return out
