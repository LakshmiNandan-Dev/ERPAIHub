#!/usr/bin/env python3
"""Export the encoder-decoder as two STATELESS ONNX graphs for portable serving.

The autoregressive loop, the graph-constraint masking, retrieval, and repair stay
in Python (they are control flow + schema logic, not tensor math). We only export
the two pure forward passes:

    encoder(src, src_keep)          -> memory          # run once per request
    decoder_step(tgt, memory, src_keep) -> next_logits # run once per token

A Python (or Triton Python-backend) driver runs encoder once, then loops
decoder_step under `SchemaPrefixGate`. onnxruntime then serves with no torch dep.

    python3 scripts/export_onnx.py --out deploy/onnx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import torch
import torch.nn as nn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm.train import load_model  # noqa: E402


class Encoder(nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, src, src_keep):
        return self.m.encode(src, src_keep)              # (B, S, d)


class DecoderStep(nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, tgt, memory, src_keep):
        tgt_keep = torch.ones_like(tgt, dtype=torch.bool)   # no padding while decoding
        h = self.m.decode(tgt, memory, src_keep, tgt_keep)
        return self.m.lm_head(h[:, -1])                  # (B, vocab) -- next-token logits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default="artifacts/model_best.pt")
    ap.add_argument("--out", default="deploy/onnx")
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    model = load_model(args.ckpt, device="cpu").eval()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    S, T, V = 24, 6, model.cfg.vocab_size
    src = torch.randint(0, V, (1, S))
    src_keep = torch.ones(1, S, dtype=torch.bool)
    memory = model.encode(src, src_keep)
    tgt = torch.randint(0, V, (1, T))

    # dynamo=False uses the legacy TorchScript exporter (no onnxscript dependency).
    common = dict(opset_version=args.opset, dynamo=False)
    torch.onnx.export(
        Encoder(model), (src, src_keep), str(out / "encoder.onnx"), **common,
        input_names=["src", "src_keep"], output_names=["memory"],
        dynamic_axes={"src": {0: "B", 1: "S"}, "src_keep": {0: "B", 1: "S"},
                      "memory": {0: "B", 1: "S"}},
    )
    torch.onnx.export(
        DecoderStep(model), (tgt, memory, src_keep), str(out / "decoder_step.onnx"), **common,
        input_names=["tgt", "memory", "src_keep"], output_names=["logits"],
        dynamic_axes={"tgt": {0: "B", 1: "T"}, "memory": {0: "B", 1: "S"},
                      "src_keep": {0: "B", 1: "S"}, "logits": {0: "B"}},
    )
    for f in ("encoder.onnx", "decoder_step.onnx"):
        print(f"  {f}: {(out / f).stat().st_size/1e6:.1f} MB")
    print(f"exported -> {out}  (vocab {V}; tokenizer.json ships alongside)")


if __name__ == "__main__":
    main()
