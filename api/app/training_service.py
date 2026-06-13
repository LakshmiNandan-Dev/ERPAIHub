"""
Local-model fine-tuning service (Ollama only).

Assembles a per-agent training set from four sources — human/AI feedback, uploaded
Q&A, RAG documents and accepted agent runs — into SFT (instruction) and DPO
(preference) examples, then emits a runnable LoRA SFT→DPO bundle (run on a GPU
host) and registers the resulting GGUF as an Ollama model. Cloud providers are
never trained here; the gateway only ever fine-tunes local models.
"""
import io
import json
import zipfile
from datetime import datetime, timezone

import httpx

from app import models

AGENTS = ("chat", "deployment", "performance", "cloning", "knowledge_base")
SOURCES = ("feedback", "upload", "rag", "agent_run")

# Per-agent system prompt baked into the resulting model's Modelfile.
_AGENT_SYSTEM = {
    "chat": "You are an Oracle E-Business Suite diagnostic assistant. Answer precisely with correct EBS/SQL.",
    "deployment": "You are an Oracle EBS code-deployment agent. Produce correct, safe deployment steps.",
    "performance": "You are an Oracle EBS performance analyst. Diagnose using AWR/ASH and DBA views.",
    "cloning": "You are an Oracle EBS cloning agent. Plan RMAN duplicate + Rapid Clone safely.",
    "knowledge_base": "You answer strictly from the provided Oracle EBS reference documentation.",
}


def system_for(agent: str) -> str:
    return _AGENT_SYSTEM.get(agent, _AGENT_SYSTEM["chat"])


def _norm(s) -> str:
    return (s or "").strip()


def _dedupe_key(agent, kind, prompt, chosen):
    return (agent, kind, _norm(prompt)[:400], _norm(chosen)[:400])


# ── Source extractors ────────────────────────────────────────────────────────────

def _feedback_examples(db):
    """Human (RLHF) + AI (RLAIF) feedback → SFT + DPO pairs. Feedback is collected on
    the chat pipeline and is broadly useful, so it is assembled into the chosen agent."""
    rows = (db.query(models.ChatMessage)
            .order_by(models.ChatMessage.session_id, models.ChatMessage.id).all())
    # map each assistant message to its immediately-preceding user prompt
    prev_user = {}
    last_user = {}
    for m in rows:
        if m.role == "user":
            last_user[m.session_id] = m.content
        elif m.role == "assistant":
            prev_user[m.id] = last_user.get(m.session_id)

    out = []
    for m in rows:
        if m.role != "assistant":
            continue
        prompt = _norm(prev_user.get(m.id))
        if not prompt:
            continue
        answer = _norm(m.content)
        # thumbs up → SFT positive
        if m.feedback_rating == 1 and answer:
            out.append({"kind": "sft", "source": "feedback", "prompt": prompt, "chosen": answer})
        # thumbs down with a human correction → DPO + SFT(corrected)
        if m.feedback_rating == -1 and _norm(m.feedback_correction):
            corr = _norm(m.feedback_correction)
            out.append({"kind": "dpo", "source": "feedback", "prompt": prompt, "chosen": corr, "rejected": answer})
            out.append({"kind": "sft", "source": "feedback", "prompt": prompt, "chosen": corr})
        # RLAIF auto-critique correction → DPO
        if m.rlaif_rating == -1 and _norm(m.rlaif_correction):
            out.append({"kind": "dpo", "source": "feedback", "prompt": prompt,
                        "chosen": _norm(m.rlaif_correction), "rejected": answer})
    return out


def _rag_examples(db, limit_docs=200, chunks_per_doc=3, max_chars=1500):
    """Ready RAG documents → SFT examples that teach the model the reference content."""
    try:
        from app.core.rag import rag_service
        coll = rag_service._get_collection()
    except Exception:
        return []
    docs = (db.query(models.RagDocument)
            .filter(models.RagDocument.status == "ready")
            .order_by(models.RagDocument.id.desc()).limit(limit_docs).all())
    out = []
    for d in docs:
        try:
            got = coll.get(where={"doc_id": str(d.id)}, limit=chunks_per_doc)
            texts = [t for t in (got.get("documents") or []) if _norm(t)]
        except Exception:
            texts = []
        if not texts:
            continue
        body = "\n\n".join(texts)[:max_chars]
        title = (d.filename or "document").rsplit(".", 1)[0]
        out.append({"kind": "sft", "source": "rag",
                    "prompt": f"From the Oracle EBS knowledge base, provide the reference content for '{title}'.",
                    "chosen": body})
    return out


def _agent_run_examples(db, agent, limit=200):
    """Accepted agent transcripts → SFT examples. Uses RLAIF-verified assistant
    messages tied to an agent run (worked, grounded examples)."""
    q = (db.query(models.ChatMessage)
         .filter(models.ChatMessage.role == "assistant",
                 models.ChatMessage.agent_run_id.isnot(None),
                 models.ChatMessage.rlaif_rating == 1)
         .order_by(models.ChatMessage.session_id, models.ChatMessage.id))
    rows = q.all()
    # rebuild preceding-user lookup
    allmsgs = (db.query(models.ChatMessage)
               .order_by(models.ChatMessage.session_id, models.ChatMessage.id).all())
    last_user, prev_user = {}, {}
    for m in allmsgs:
        if m.role == "user":
            last_user[m.session_id] = m.content
        elif m.role == "assistant":
            prev_user[m.id] = last_user.get(m.session_id)
    out = []
    for m in rows[:limit]:
        prompt = _norm(prev_user.get(m.id))
        answer = _norm(m.content)
        if prompt and answer:
            out.append({"kind": "sft", "source": "agent_run", "prompt": prompt, "chosen": answer})
    return out


def assemble(db, agent: str, sources, created_by=None) -> dict:
    """Generate + persist TrainingExample rows for an agent from the chosen sources.
    'upload' rows are added via the upload endpoint, not regenerated here. Returns
    a per-source count of newly added examples (existing/duplicate rows are skipped)."""
    sources = [s for s in sources if s in SOURCES]
    generated = []
    if "feedback" in sources:
        generated += _feedback_examples(db)
    if "rag" in sources:
        generated += _rag_examples(db)
    if "agent_run" in sources:
        generated += _agent_run_examples(db, agent)

    # existing keys for dedupe
    existing = {_dedupe_key(e.agent, e.kind, e.prompt, e.chosen)
                for e in db.query(models.TrainingExample).filter(models.TrainingExample.agent == agent).all()}
    added = {"feedback": 0, "rag": 0, "agent_run": 0}
    for ex in generated:
        if ex["kind"] == "dpo" and not _norm(ex.get("rejected")):
            continue
        key = _dedupe_key(agent, ex["kind"], ex["prompt"], ex["chosen"])
        if key in existing:
            continue
        existing.add(key)
        db.add(models.TrainingExample(
            agent=agent, kind=ex["kind"], source=ex["source"],
            system=system_for(agent), prompt=ex["prompt"], chosen=ex["chosen"],
            rejected=ex.get("rejected"), created_by=created_by))
        added[ex["source"]] = added.get(ex["source"], 0) + 1
    db.commit()
    return {"added": sum(added.values()), "by_source": added}


def counts(db, agent: str) -> dict:
    rows = (db.query(models.TrainingExample)
            .filter(models.TrainingExample.agent == agent,
                    models.TrainingExample.is_active == True).all())  # noqa: E712
    by_source = {}
    sft = dpo = 0
    for r in rows:
        by_source[r.source] = by_source.get(r.source, 0) + 1
        if r.kind == "dpo":
            dpo += 1
        else:
            sft += 1
    return {"total": len(rows), "sft": sft, "dpo": dpo, "by_source": by_source}


# ── Bundle artefacts ─────────────────────────────────────────────────────────────

def _sft_jsonl(examples) -> str:
    lines = []
    for e in examples:
        if e.kind != "sft":
            continue
        lines.append(json.dumps({"system": e.system or "", "prompt": e.prompt, "completion": e.chosen}))
    return "\n".join(lines) + ("\n" if lines else "")


def _dpo_jsonl(examples) -> str:
    lines = []
    for e in examples:
        if e.kind != "dpo" or not _norm(e.rejected):
            continue
        lines.append(json.dumps({"system": e.system or "", "prompt": e.prompt,
                                 "chosen": e.chosen, "rejected": e.rejected}))
    return "\n".join(lines) + ("\n" if lines else "")


def _modelfile(base_model: str, system: str) -> str:
    return (f"# Ollama Modelfile — import the fine-tuned GGUF produced by train.py\n"
            f"FROM ./model.gguf\n"
            f'SYSTEM """{system}"""\n'
            f"PARAMETER temperature 0.3\n"
            f"PARAMETER num_ctx 8192\n"
            f"# Base model this was fine-tuned from (for provenance): {base_model}\n")


def _train_py() -> str:
    return r'''#!/usr/bin/env python
"""
LoRA fine-tune (SFT -> DPO) for an OraEBS Agent local model. Run on a GPU host.

  pip install -r requirements.txt
  python train.py --base <hf_base_repo> --out ./out

Then convert the merged model to GGUF (llama.cpp) and `ollama create` it, or upload
the GGUF back into the OraEBS Training console which will register it for you.
"""
import argparse, json, os

import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import LoraConfig, get_peft_model
from trl import SFTConfig, SFTTrainer, DPOConfig, DPOTrainer


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="HF base repo, e.g. meta-llama/Llama-3.2-1B-Instruct")
    ap.add_argument("--out", default="./out")
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--lora_r", type=int, default=16)
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(args.base)
    tok.pad_token = tok.pad_token or tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(args.base, torch_dtype=torch.bfloat16, device_map="auto")
    lora = LoraConfig(r=args.lora_r, lora_alpha=args.lora_r * 2, lora_dropout=0.05,
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj"], task_type="CAUSAL_LM")
    model = get_peft_model(model, lora)

    def fmt(s, p, c):
        sys = (f"<|system|>\n{s}\n" if s else "")
        return f"{sys}<|user|>\n{p}\n<|assistant|>\n{c}{tok.eos_token}"

    sft = read_jsonl("sft.jsonl")
    if sft:
        ds = Dataset.from_list([{"text": fmt(r.get("system",""), r["prompt"], r["completion"])} for r in sft])
        SFTTrainer(model=model, train_dataset=ds, args=SFTConfig(
            output_dir=args.out, num_train_epochs=args.epochs, learning_rate=args.lr,
            per_device_train_batch_size=1, gradient_accumulation_steps=8, logging_steps=10,
            save_strategy="epoch", bf16=True, max_length=2048)).train()

    dpo = read_jsonl("dpo.jsonl")
    if dpo:
        ds = Dataset.from_list([{"prompt": (f"{r.get('system','')}\n\n{r['prompt']}").strip(),
                                 "chosen": r["chosen"], "rejected": r["rejected"]} for r in dpo])
        DPOTrainer(model=model, train_dataset=ds, processing_class=tok, args=DPOConfig(
            output_dir=args.out + "_dpo", num_train_epochs=max(1.0, args.epochs / 2), learning_rate=args.lr / 4,
            per_device_train_batch_size=1, gradient_accumulation_steps=8, logging_steps=10,
            beta=0.1, bf16=True)).train()

    merged = model.merge_and_unload()
    merged.save_pretrained(args.out + "_merged"); tok.save_pretrained(args.out + "_merged")
    print("Merged model in", args.out + "_merged")
    print("Next: python llama.cpp/convert_hf_to_gguf.py", args.out + "_merged",
          "--outfile model.gguf --outtype q8_0   # then `ollama create <tag> -f Modelfile`")


if __name__ == "__main__":
    main()
'''


def _requirements() -> str:
    return "torch\ntransformers>=4.44\ndatasets\npeft>=0.12\ntrl>=0.11\naccelerate\nbitsandbytes\nsentencepiece\n"


def _readme(job, n_sft, n_dpo, base_model, tag, system) -> str:
    return (
        f"# OraEBS Agent — Training Bundle ({job.agent} agent)\n\n"
        f"Fine-tune a **local** model and register it in Ollama as `{tag}`.\n\n"
        f"- **Base model:** `{base_model}` (use the matching Hugging Face repo for `--base`).\n"
        f"- **Method:** {job.method.upper()} — SFT examples: **{n_sft}**, DPO pairs: **{n_dpo}**.\n"
        f"- **Target agent:** `{job.agent}` (cloud models are never trained — this is local-only).\n\n"
        "## Files\n"
        "- `sft.jsonl` — instruction/answer pairs (system, prompt, completion).\n"
        "- `dpo.jsonl` — preference pairs (system, prompt, chosen, rejected).\n"
        "- `train.py` — LoRA SFT then DPO; merges adapters into the base.\n"
        "- `requirements.txt`, `Modelfile`.\n\n"
        "## Run (on a GPU host)\n"
        "```bash\n"
        "pip install -r requirements.txt\n"
        "python train.py --base <hf_base_repo> --out ./out --epochs 3\n"
        "# convert the merged model to GGUF with llama.cpp, name it model.gguf, then either:\n"
        f"ollama create {tag} -f Modelfile      # register directly on the Ollama host\n"
        "# ...or upload model.gguf in the OraEBS Training console to register it for you.\n"
        "```\n\n"
        f"After registration the **{job.agent}** agent will use `{tag}` (Admin Console → Training → "
        "Per-agent models). Only the Ollama provider is affected.\n\n"
        f"System prompt baked into the model:\n\n> {system}\n"
    )


def build_bundle_zip(job, examples) -> tuple[bytes, str]:
    sft = _sft_jsonl(examples)
    dpo = _dpo_jsonl(examples)
    n_sft = sft.count("\n") if sft.strip() else 0
    n_dpo = dpo.count("\n") if dpo.strip() else 0
    system = system_for(job.agent)
    root = f"oraebs_train_{job.id}_{job.agent}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if job.method in ("sft", "both"):
            z.writestr(f"{root}/sft.jsonl", sft)
        if job.method in ("dpo", "both"):
            z.writestr(f"{root}/dpo.jsonl", dpo)
        info = zipfile.ZipInfo(f"{root}/train.py"); info.external_attr = 0o100755 << 16
        z.writestr(info, _train_py())
        z.writestr(f"{root}/requirements.txt", _requirements())
        z.writestr(f"{root}/Modelfile", _modelfile(job.base_model, system))
        z.writestr(f"{root}/README.md", _readme(job, n_sft, n_dpo, job.base_model, job.target_model_tag, system))
        z.writestr(f"{root}/manifest.json", json.dumps({
            "job_id": job.id, "agent": job.agent, "base_model": job.base_model,
            "method": job.method, "target_model_tag": job.target_model_tag,
            "sft_count": n_sft, "dpo_count": n_dpo,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
    return buf.getvalue(), f"{root}.zip"


# ── Ollama registration ──────────────────────────────────────────────────────────

def register_in_ollama(base_url: str, tag: str, gguf_path: str, system: str) -> dict:
    """Create an Ollama model from a GGUF already present on the Ollama host.
    `gguf_path` must be a path the Ollama server can read (e.g. on a mounted volume).
    Returns {ok, message}."""
    modelfile = f'FROM {gguf_path}\nSYSTEM """{system}"""\nPARAMETER temperature 0.3\n'
    url = f"{base_url.rstrip('/')}/api/create"
    try:
        with httpx.Client(timeout=600.0) as client:
            # Newer Ollama accepts {"model","modelfile"}; older accepts {"name","modelfile"}.
            resp = client.post(url, json={"model": tag, "name": tag, "modelfile": modelfile})
            if resp.status_code >= 400:
                return {"ok": False, "message": f"Ollama create failed (HTTP {resp.status_code}): {resp.text[:300]}"}
        return {"ok": True, "message": f"Registered Ollama model '{tag}' from {gguf_path}."}
    except Exception as exc:
        return {"ok": False, "message": f"Could not reach Ollama to create the model: {exc}"}
