"""Byte-level BPE tokenizer -- built from scratch (no external tokenizer libs).

Byte-level => no out-of-vocabulary ever (any string round-trips exactly).
Trained on our own corpus (serialized schema + questions + SQL) so EBS
identifiers (`gl_code_combinations`, `segment2`) and SQL keywords (`SELECT`,
`GROUP BY`) become efficient single/few tokens.

Training operates on UNIQUE pre-token chunk frequencies (the standard BPE
optimization): the number of distinct words/identifiers is small, so learning
thousands of merges stays fast in pure Python. We can swap in a compiled
backend later behind this same interface -- performance only.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

# Identifiers (keep `segment1`, `_headers_all` whole) | numbers | spaces | punctuation.
_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*|\d+|\s+|[^\sA-Za-z0-9_]"

# Structural / control tokens used to frame encoder input and decoder target.
DEFAULT_SPECIALS = ("<pad>", "<bos>", "<eos>", "<question>", "<schema>", "<sql>")


class BPETokenizer:
    def __init__(self, pattern: str = _PATTERN):
        self.pattern_str = pattern
        self.pattern = re.compile(pattern)
        self.merges: dict[tuple[int, int], int] = {}
        self.vocab: dict[int, bytes] = {i: bytes([i]) for i in range(256)}
        self.special_tokens: dict[str, int] = {}
        self._inv_special: dict[int, str] = {}

    # -- training --------------------------------------------------------
    def train(self, texts, vocab_size: int, special_tokens=DEFAULT_SPECIALS):
        assert vocab_size >= 256
        num_merges = vocab_size - 256

        freq: Counter[bytes] = Counter()
        for text in texts:
            for piece in self.pattern.findall(text):
                freq[piece.encode("utf-8")] += 1
        chunks = [(list(b), c) for b, c in freq.items()]

        vocab = {i: bytes([i]) for i in range(256)}
        merges: dict[tuple[int, int], int] = {}
        for i in range(num_merges):
            stats: dict[tuple[int, int], int] = {}
            for ids, count in chunks:
                for pair in zip(ids, ids[1:]):
                    stats[pair] = stats.get(pair, 0) + count
            if not stats:
                break
            best = max(stats, key=stats.get)
            if stats[best] < 2:  # nothing repeats -> no useful merges left
                break
            idx = 256 + i
            chunks = [(_merge(ids, best, idx), c) for ids, c in chunks]
            merges[best] = idx
            vocab[idx] = vocab[best[0]] + vocab[best[1]]

        self.merges = merges
        self.vocab = vocab
        self._register_specials(special_tokens, base=256 + len(merges))
        return self

    def _register_specials(self, specials, base: int):
        self.special_tokens = {tok: base + j for j, tok in enumerate(specials)}
        self._inv_special = {v: k for k, v in self.special_tokens.items()}

    # -- encode / decode -------------------------------------------------
    def encode_ordinary(self, text: str) -> list[int]:
        ids: list[int] = []
        for piece in self.pattern.findall(text):
            ids.extend(self._encode_chunk(piece.encode("utf-8")))
        return ids

    def encode(self, text: str, allow_special: bool = True) -> list[int]:
        if not (allow_special and self.special_tokens):
            return self.encode_ordinary(text)
        specials = sorted(self.special_tokens, key=len, reverse=True)
        splitter = "(" + "|".join(re.escape(s) for s in specials) + ")"
        ids: list[int] = []
        for part in re.split(splitter, text):
            if part in self.special_tokens:
                ids.append(self.special_tokens[part])
            elif part:
                ids.extend(self.encode_ordinary(part))
        return ids

    def decode(self, ids) -> str:
        out: list[bytes] = []
        for i in ids:
            if i in self._inv_special:
                out.append(self._inv_special[i].encode("utf-8"))
            else:
                out.append(self.vocab[i])
        return b"".join(out).decode("utf-8", errors="replace")

    def _encode_chunk(self, raw: bytes) -> list[int]:
        ids = list(raw)
        while len(ids) >= 2:
            pair = min(zip(ids, ids[1:]), key=lambda p: self.merges.get(p, float("inf")))
            if pair not in self.merges:
                break
            ids = _merge(ids, pair, self.merges[pair])
        return ids

    # -- convenience -----------------------------------------------------
    @property
    def vocab_size(self) -> int:
        return 256 + len(self.merges) + len(self.special_tokens)

    def special(self, name: str) -> int:
        return self.special_tokens[name]

    # -- persistence -----------------------------------------------------
    def save(self, path: str | Path):
        data = {
            "pattern": self.pattern_str,
            "merges": [[a, b, idx] for (a, b), idx in self.merges.items()],
            "specials": list(self.special_tokens),
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data))

    @classmethod
    def load(cls, path: str | Path) -> "BPETokenizer":
        data = json.loads(Path(path).read_text())
        tok = cls(pattern=data["pattern"])
        merges, vocab = {}, {i: bytes([i]) for i in range(256)}
        for a, b, idx in sorted(data["merges"], key=lambda r: r[2]):
            merges[(a, b)] = idx
            vocab[idx] = vocab[a] + vocab[b]
        tok.merges, tok.vocab = merges, vocab
        tok._register_specials(data["specials"], base=256 + len(merges))
        return tok


def _merge(ids: list[int], pair: tuple[int, int], idx: int) -> list[int]:
    out, i = [], 0
    while i < len(ids):
        if i < len(ids) - 1 and ids[i] == pair[0] and ids[i + 1] == pair[1]:
            out.append(idx)
            i += 2
        else:
            out.append(ids[i])
            i += 1
    return out
