"""Turn (question, schema, SQL) into padded tensors + masks for the model.

Source  : <question> <q tokens> <schema> <schema tokens>
Target  : <bos> <sql tokens>            (decoder input)
Labels  :       <sql tokens> <eos>      (shifted -> teacher forcing)
"""

from __future__ import annotations

import torch


def encode_pair(tok, question: str, schema_str: str, sql: str):
    src = (
        [tok.special("<question>")]
        + tok.encode(question, allow_special=False)
        + [tok.special("<schema>")]
        + tok.encode(schema_str, allow_special=False)
    )
    sql_ids = tok.encode(sql, allow_special=False)
    tgt_in = [tok.special("<bos>")] + sql_ids
    labels = sql_ids + [tok.special("<eos>")]
    return src, tgt_in, labels


def collate(pairs, tok, device="cpu"):
    pad = tok.special("<pad>")
    enc = [encode_pair(tok, q, s, sql) for q, s, sql in pairs]
    s_len = max(len(e[0]) for e in enc)
    t_len = max(len(e[1]) for e in enc)

    def pad_to(seq, n):
        return seq + [pad] * (n - len(seq))

    src = torch.tensor([pad_to(e[0], s_len) for e in enc], dtype=torch.long)
    tgt_in = torch.tensor([pad_to(e[1], t_len) for e in enc], dtype=torch.long)
    labels = torch.tensor([pad_to(e[2], t_len) for e in enc], dtype=torch.long)
    batch = {
        "src": src,
        "tgt_in": tgt_in,
        "labels": labels,
        "src_keep": src != pad,
        "tgt_keep": tgt_in != pad,
    }
    return {k: v.to(device) for k, v in batch.items()}
