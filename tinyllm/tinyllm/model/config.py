from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def _default_d_ff(d_model: int) -> int:
    """SwiGLU hidden ~ 8/3 * d_model, rounded to a multiple of 64."""
    h = int(8 / 3 * d_model)
    return ((h + 63) // 64) * 64


@dataclass
class ModelConfig:
    vocab_size: int
    d_model: int = 256
    n_heads: int = 8
    n_enc_layers: int = 4
    n_dec_layers: int = 4
    d_ff: Optional[int] = None
    max_seq_len: int = 512
    dropout: float = 0.0
    pad_id: int = 0
    rope_theta: float = 10_000.0
    tie_embeddings: bool = True

    def __post_init__(self):
        if self.d_ff is None:
            self.d_ff = _default_d_ff(self.d_model)
        assert self.d_model % self.n_heads == 0, "d_model must divide n_heads"
        assert (self.d_model // self.n_heads) % 2 == 0, "head_dim must be even for RoPE"

    @property
    def head_dim(self) -> int:
        return self.d_model // self.n_heads
