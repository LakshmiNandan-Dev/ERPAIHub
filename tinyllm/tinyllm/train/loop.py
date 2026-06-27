"""Training loop: batching, warmup+cosine LR, grad clip, periodic eval, ckpt.

Eval metrics (on UNSEEN schemas):
  val_loss     teacher-forced cross-entropy
  token_acc    teacher-forced next-token accuracy
  exact_match  greedy-decoded SQL == gold (the headline generalization metric)
  valid_sql    greedy-decoded SQL parses as Oracle (sqlglot)

Execution accuracy (run the SQL, compare result sets) is the eventual upgrade
once Oracle XE is wired in; exact_match + valid_sql are the no-infra proxies.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import torch

from ..model import EncoderDecoder, ModelConfig, collate
from ..validate import validate_sqlglot


@dataclass
class TrainConfig:
    total_steps: int = 600
    batch_size: int = 48
    lr: float = 5e-4
    min_lr: float = 5e-5
    warmup: int = 50
    weight_decay: float = 0.1
    grad_clip: float = 1.0
    eval_every: int = 150
    log_every: int = 50
    n_decode: int = 64
    decode_max_len: int = 192
    eval_batch: int = 32          # mini-batch eval so big val sets don't OOM (esp. MPS)
    ckpt_dir: str = "artifacts"
    device: str = "cpu"


class Trainer:
    def __init__(self, model, tok, train_pairs, val_pairs, cfg: TrainConfig):
        self.model = model.to(cfg.device)
        self.tok = tok
        self.train_pairs = train_pairs
        self.val_pairs = val_pairs
        self.cfg = cfg
        self.pad = tok.special("<pad>")
        self.opt = torch.optim.AdamW(
            model.parameters(), lr=cfg.lr, betas=(0.9, 0.95), weight_decay=cfg.weight_decay
        )

    def _lr(self, step: int) -> float:
        c = self.cfg
        if step < c.warmup:
            return c.lr * (step + 1) / c.warmup
        prog = (step - c.warmup) / max(1, c.total_steps - c.warmup)
        return c.min_lr + 0.5 * (c.lr - c.min_lr) * (1 + math.cos(math.pi * prog))

    def train(self):
        cfg = self.cfg
        rng = random.Random(0)
        step, best_em = 0, -1.0
        t0 = time.time()
        self.model.train()
        while step < cfg.total_steps:
            order = list(range(len(self.train_pairs)))
            rng.shuffle(order)
            for start in range(0, len(order), cfg.batch_size):
                if step >= cfg.total_steps:
                    break
                idx = order[start:start + cfg.batch_size]
                batch = collate([self.train_pairs[i] for i in idx], self.tok, cfg.device)

                lr = self._lr(step)
                for g in self.opt.param_groups:
                    g["lr"] = lr
                _, loss = self.model(batch["src"], batch["tgt_in"],
                                     batch["src_keep"], batch["tgt_keep"], batch["labels"])
                self.opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), cfg.grad_clip)
                self.opt.step()
                step += 1

                if step % cfg.log_every == 0:
                    print(f"  step {step:4d}/{cfg.total_steps}  loss {loss.item():.4f}  "
                          f"lr {lr:.2e}  ({step/(time.time()-t0):.1f} steps/s)")
                if step % cfg.eval_every == 0 or step == cfg.total_steps:
                    m = self.evaluate()
                    print(f"  [eval @ {step}] val_loss {m['val_loss']:.3f}  "
                          f"token_acc {m['token_acc']:.3f}  exact {m['exact_match']:.3f}  "
                          f"valid_sql {m['valid_sql']:.3f}")
                    if m["exact_match"] >= best_em:
                        best_em = m["exact_match"]
                        self.save_checkpoint(Path(cfg.ckpt_dir) / "model_best.pt", step, m)
                    self.model.train()
        print(f"done. best exact_match {best_em:.3f} in {time.time()-t0:.0f}s")
        return best_em

    @torch.no_grad()
    def evaluate(self):
        self.model.eval()
        bs, pad = self.cfg.eval_batch, self.pad
        _empty_cache(self.cfg.device)

        # teacher-forced metrics, mini-batched (token-weighted averages)
        loss_sum, tok_total, correct = 0.0, 0, 0
        for i in range(0, min(256, len(self.val_pairs)), bs):
            b = collate(self.val_pairs[i:i + bs], self.tok, self.cfg.device)
            logits, loss = self.model(b["src"], b["tgt_in"], b["src_keep"],
                                      b["tgt_keep"], b["labels"])
            mask = b["labels"] != pad
            n = int(mask.sum())
            loss_sum += loss.item() * n
            tok_total += n
            correct += int((logits.argmax(-1)[mask] == b["labels"][mask]).sum())
        val_loss = loss_sum / max(1, tok_total)
        token_acc = correct / max(1, tok_total)

        # greedy decode on a subset, mini-batched
        bos, eos = self.tok.special("<bos>"), self.tok.special("<eos>")
        sub = self.val_pairs[: self.cfg.n_decode]
        exact = valid = 0
        for i in range(0, len(sub), bs):
            chunk = sub[i:i + bs]
            db = collate(chunk, self.tok, self.cfg.device)
            gen = self.model.generate(db["src"], db["src_keep"], bos, eos,
                                      max_len=self.cfg.decode_max_len).tolist()
            for row, (_, _, gold) in zip(gen, chunk):
                if eos in row:
                    row = row[: row.index(eos)]
                pred = self.tok.decode(row[1:])
                exact += int(pred.strip() == gold.strip())
                valid += int(validate_sqlglot(pred).ok is True)
        _empty_cache(self.cfg.device)
        n = len(sub)
        return {"val_loss": val_loss, "token_acc": token_acc,
                "exact_match": exact / n, "valid_sql": valid / n}

    def save_checkpoint(self, path, step, metrics):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {"model": self.model.state_dict(), "config": asdict(self.model.cfg),
             "step": step, "metrics": metrics},
            path,
        )
        self.tok.save(path.parent / "tokenizer.json")


def _empty_cache(device: str):
    if device == "mps":
        try:
            torch.mps.empty_cache()
        except Exception:
            pass
    elif device == "cuda":
        torch.cuda.empty_cache()


def load_model(path, device="cpu") -> EncoderDecoder:
    ckpt = torch.load(path, map_location=device, weights_only=False)
    model = EncoderDecoder(ModelConfig(**ckpt["config"])).to(device)
    model.load_state_dict(ckpt["model"])
    return model
