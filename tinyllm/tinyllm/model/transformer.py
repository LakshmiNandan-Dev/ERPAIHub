"""From-scratch encoder-decoder transformer for NL -> Oracle SQL.

Modern recipe, kept compact:
  - RMSNorm (pre-norm)        - RoPE in self-attention (not cross)
  - SwiGLU MLP                - multi-head self + cross attention
  - 3-way tied embeddings     - encoder bidirectional, decoder causal + cross

The encoder reads (question + serialized schema) bidirectionally; the decoder
generates SQL causally while cross-attending to the encoder memory.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from .config import ModelConfig

_NEG = -1e9


class RMSNorm(nn.Module):
    def __init__(self, d: int, eps: float = 1e-6):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(d))
        self.eps = eps

    def forward(self, x):
        norm = x.float() * torch.rsqrt(x.float().pow(2).mean(-1, keepdim=True) + self.eps)
        return (norm.type_as(x)) * self.weight


def _rotate_half(x):
    x1, x2 = x.chunk(2, dim=-1)
    return torch.cat((-x2, x1), dim=-1)


def build_rope(seq_len: int, head_dim: int, theta: float, device, dtype):
    inv_freq = 1.0 / (theta ** (torch.arange(0, head_dim, 2, device=device).float() / head_dim))
    t = torch.arange(seq_len, device=device).float()
    freqs = torch.outer(t, inv_freq)               # (T, head_dim/2)
    emb = torch.cat((freqs, freqs), dim=-1)        # (T, head_dim)
    return emb.cos()[None, None].to(dtype), emb.sin()[None, None].to(dtype)


def _apply_rope(x, cos, sin):
    return x * cos + _rotate_half(x) * sin


class Attention(nn.Module):
    def __init__(self, cfg: ModelConfig):
        super().__init__()
        self.h, self.dh = cfg.n_heads, cfg.head_dim
        d = cfg.d_model
        self.wq = nn.Linear(d, d, bias=False)
        self.wk = nn.Linear(d, d, bias=False)
        self.wv = nn.Linear(d, d, bias=False)
        self.wo = nn.Linear(d, d, bias=False)
        self.dropout = cfg.dropout

    def forward(self, xq, xkv, attn_mask, rope=None):
        B, Tq, _ = xq.shape
        Tk = xkv.size(1)
        q = self.wq(xq).view(B, Tq, self.h, self.dh).transpose(1, 2)
        k = self.wk(xkv).view(B, Tk, self.h, self.dh).transpose(1, 2)
        v = self.wv(xkv).view(B, Tk, self.h, self.dh).transpose(1, 2)
        if rope is not None:
            cos, sin = rope
            q, k = _apply_rope(q, cos, sin), _apply_rope(k, cos, sin)
        out = F.scaled_dot_product_attention(
            q, k, v, attn_mask=attn_mask,
            dropout_p=self.dropout if self.training else 0.0,
        )
        out = out.transpose(1, 2).reshape(B, Tq, self.h * self.dh)
        return self.wo(out)


class SwiGLU(nn.Module):
    def __init__(self, d: int, d_ff: int):
        super().__init__()
        self.w_gate = nn.Linear(d, d_ff, bias=False)
        self.w_up = nn.Linear(d, d_ff, bias=False)
        self.w_down = nn.Linear(d_ff, d, bias=False)

    def forward(self, x):
        return self.w_down(F.silu(self.w_gate(x)) * self.w_up(x))


class EncoderBlock(nn.Module):
    def __init__(self, cfg: ModelConfig):
        super().__init__()
        self.norm1, self.attn = RMSNorm(cfg.d_model), Attention(cfg)
        self.norm2, self.mlp = RMSNorm(cfg.d_model), SwiGLU(cfg.d_model, cfg.d_ff)
        self.drop = nn.Dropout(cfg.dropout)        # param-free -> ckpt-compatible

    def forward(self, x, src_mask, rope):
        h = self.norm1(x)
        x = x + self.drop(self.attn(h, h, src_mask, rope))
        return x + self.drop(self.mlp(self.norm2(x)))


class DecoderBlock(nn.Module):
    def __init__(self, cfg: ModelConfig):
        super().__init__()
        self.norm1, self.self_attn = RMSNorm(cfg.d_model), Attention(cfg)
        self.norm2, self.cross_attn = RMSNorm(cfg.d_model), Attention(cfg)
        self.norm3, self.mlp = RMSNorm(cfg.d_model), SwiGLU(cfg.d_model, cfg.d_ff)
        self.drop = nn.Dropout(cfg.dropout)

    def forward(self, x, memory, self_mask, cross_mask, rope):
        h = self.norm1(x)
        x = x + self.drop(self.self_attn(h, h, self_mask, rope))
        x = x + self.drop(self.cross_attn(self.norm2(x), memory, cross_mask))  # rope=None for cross
        return x + self.drop(self.mlp(self.norm3(x)))


class EncoderDecoder(nn.Module):
    def __init__(self, cfg: ModelConfig):
        super().__init__()
        self.cfg = cfg
        self.embed = nn.Embedding(cfg.vocab_size, cfg.d_model)
        self.enc_layers = nn.ModuleList(EncoderBlock(cfg) for _ in range(cfg.n_enc_layers))
        self.dec_layers = nn.ModuleList(DecoderBlock(cfg) for _ in range(cfg.n_dec_layers))
        self.enc_norm = RMSNorm(cfg.d_model)
        self.dec_norm = RMSNorm(cfg.d_model)
        self.lm_head = nn.Linear(cfg.d_model, cfg.vocab_size, bias=False)
        self.apply(self._init)
        if cfg.tie_embeddings:
            self.lm_head.weight = self.embed.weight  # 3-way tie (enc-in/dec-in/dec-out)

    @staticmethod
    def _init(m):
        if isinstance(m, (nn.Linear, nn.Embedding)):
            nn.init.normal_(m.weight, mean=0.0, std=0.02)

    # -- masks (additive float, broadcastable to B,H,Tq,Tk) -------------
    @staticmethod
    def _pad_mask(keep):  # keep: (B, Tk) bool -> (B,1,1,Tk) additive
        m = torch.zeros_like(keep, dtype=torch.float32)
        return m.masked_fill(~keep, _NEG)[:, None, None, :]

    def _causal_mask(self, tgt_keep):  # -> (B,1,T,T)
        B, T = tgt_keep.shape
        causal = torch.tril(torch.ones(T, T, dtype=torch.bool, device=tgt_keep.device))
        keep = causal[None, None] & tgt_keep[:, None, None, :]
        return torch.zeros(B, 1, T, T, device=tgt_keep.device).masked_fill(~keep, _NEG)

    def _rope(self, seq_len, device, dtype):
        return build_rope(seq_len, self.cfg.head_dim, self.cfg.rope_theta, device, dtype)

    # -- forward ---------------------------------------------------------
    def encode(self, src, src_keep):
        x = self.embed(src)
        rope = self._rope(src.size(1), src.device, x.dtype)
        mask = self._pad_mask(src_keep)
        for layer in self.enc_layers:
            x = layer(x, mask, rope)
        return self.enc_norm(x)

    def decode(self, tgt, memory, src_keep, tgt_keep):
        x = self.embed(tgt)
        rope = self._rope(tgt.size(1), tgt.device, x.dtype)
        self_mask = self._causal_mask(tgt_keep)
        cross_mask = self._pad_mask(src_keep)
        for layer in self.dec_layers:
            x = layer(x, memory, self_mask, cross_mask, rope)
        return self.dec_norm(x)

    def forward(self, src, tgt_in, src_keep, tgt_keep, labels=None):
        memory = self.encode(src, src_keep)
        h = self.decode(tgt_in, memory, src_keep, tgt_keep)
        logits = self.lm_head(h)
        loss = None
        if labels is not None:
            loss = F.cross_entropy(
                logits.reshape(-1, logits.size(-1)),
                labels.reshape(-1),
                ignore_index=self.cfg.pad_id,
            )
        return logits, loss

    @torch.no_grad()
    def generate(self, src, src_keep, bos_id, eos_id, max_len=128):
        self.eval()
        memory = self.encode(src, src_keep)
        B = src.size(0)
        tgt = torch.full((B, 1), bos_id, dtype=torch.long, device=src.device)
        done = torch.zeros(B, dtype=torch.bool, device=src.device)
        for _ in range(max_len):
            tgt_keep = torch.ones_like(tgt, dtype=torch.bool)
            h = self.decode(tgt, memory, src_keep, tgt_keep)
            nxt = self.lm_head(h[:, -1]).argmax(-1, keepdim=True)
            tgt = torch.cat([tgt, nxt], dim=1)
            done = done | (nxt.squeeze(1) == eos_id)
            if done.all():
                break
        return tgt

    def num_params(self) -> int:
        seen, total = set(), 0
        for p in self.parameters():
            if id(p) not in seen:        # count tied weights once
                seen.add(id(p))
                total += p.numel()
        return total
