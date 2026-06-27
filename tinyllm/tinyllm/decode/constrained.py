"""Graph-constrained decoding (PICARD-style, generate-and-verify).

The locked Option 3a: the model proposes, the GRAPH validates/re-ranks.
We beam-search K candidates and accept the best that is both grammar-valid
(parses as Oracle) and schema-valid (`graph_check_sql`: tables/columns exist,
joins follow real FK edges). If none pass, we fall back to the top beam.

`graph_check_sql` is the inference-time gate from our design, operating on the
model's *string* output (parsed by sqlglot) -- the same structural guarantee as
`validate_graph` does on our own AST during data generation.
"""

from __future__ import annotations

import re

import torch

from ..schema_graph import SchemaGraph
from ..validate import validate_sqlglot
from ..validate.result import ValidationResult

_NEG = -1e9   # additive mask value for disallowed tokens


def graph_check_sql(sql: str, graph: SchemaGraph) -> ValidationResult:
    """Structural check of generated SQL against the schema graph."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return ValidationResult("graph_sql", ok=None, issues=["sqlglot not installed"])

    try:
        tree = sqlglot.parse_one(sql, dialect="oracle")
    except Exception as e:  # parse error -> not grammar-valid
        return ValidationResult("graph_sql", ok=False, issues=[f"parse: {str(e).splitlines()[0]}"])
    if tree is None:
        return ValidationResult("graph_sql", ok=False, issues=["empty parse"])

    schema = graph.schema
    issues: list[str] = []

    # alias -> real table name (collected across all scopes)
    alias: dict[str, str] = {}
    for t in tree.find_all(exp.Table):
        alias[t.alias or t.name] = t.name
        alias[t.name] = t.name

    for name in set(alias.values()):
        if schema.table(name) is None:
            issues.append(f"unknown table {name}")

    for col in tree.find_all(exp.Column):
        q = col.table  # qualifier (alias or table); '' if unqualified
        if not q:
            continue
        tbl = alias.get(q)
        if tbl is None:
            continue  # unresolved qualifier (e.g. subquery alias) -> be lenient
        t = schema.table(tbl)
        if t is not None and t.column(col.name) is None:
            issues.append(f"unknown column {tbl}.{col.name}")

    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is None:
            continue
        for eq in on.find_all(exp.EQ):
            left, right = eq.left, eq.right
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            tl, tr = alias.get(left.table), alias.get(right.table)
            if tl is None or tr is None:
                continue
            fk = graph.fk_between(tl, tr)
            if fk is None:
                issues.append(f"join {tl}<->{tr} not an FK edge")
            else:
                keys = {
                    (fk.from_table, fk.from_column, fk.to_table, fk.to_column),
                    (fk.to_table, fk.to_column, fk.from_table, fk.from_column),
                }
                if (tl, left.name, tr, right.name) not in keys:
                    issues.append(f"join key {tl}.{left.name}={tr}.{right.name} not the FK")

    return ValidationResult("graph_sql", ok=(len(issues) == 0), issues=issues)


@torch.no_grad()
def beam_search(model, src, src_keep, bos, eos, beam=5, max_len=160, len_penalty=0.7):
    """Single-example beam search. Returns [(tokens, score)] best-first."""
    model.eval()
    device = src.device
    memory = model.encode(src, src_keep)            # (1, S, d)
    beams: list[tuple[list[int], float]] = [([bos], 0.0)]
    finished: list[tuple[list[int], float]] = []

    def norm(item):
        return item[1] / (len(item[0]) ** len_penalty)

    for _ in range(max_len):
        live = [b for b in beams if b[0][-1] != eos]
        finished.extend(b for b in beams if b[0][-1] == eos)
        if not live:
            break
        cands: list[tuple[list[int], float]] = []
        for tokens, score in live:
            tgt = torch.tensor([tokens], device=device)
            h = model.decode(tgt, memory, src_keep, torch.ones_like(tgt, dtype=torch.bool))
            logp = torch.log_softmax(model.lm_head(h[:, -1]), dim=-1)[0]
            vals, idx = logp.topk(beam)
            for v, i in zip(vals.tolist(), idx.tolist()):
                cands.append((tokens + [i], score + v))
        cands.sort(key=norm, reverse=True)
        beams = cands[:beam]
    finished.extend(beams)
    finished.sort(key=norm, reverse=True)
    return finished


def constrained_generate(model, tok, src, src_keep, schema, beam=5, max_len=160):
    """Beam-search then accept the best grammar+graph-valid candidate.

    Returns (sql, constrained) where constrained=False means we fell back to the
    top beam because nothing passed the graph gate.
    """
    bos, eos = tok.special("<bos>"), tok.special("<eos>")
    graph = SchemaGraph(schema)
    cands = beam_search(model, src, src_keep, bos, eos, beam=beam, max_len=max_len)

    def to_sql(tokens):
        body = tokens[1:]
        if eos in body:
            body = body[: body.index(eos)]
        return tok.decode(body)

    for tokens, _ in cands:
        sql = to_sql(tokens)
        if validate_sqlglot(sql).ok and graph_check_sql(sql, graph).ok:
            return sql, True
    return to_sql(cands[0][0]), False


# -- incremental (PICARD-style) constrained decoding -----------------------
#
# Instead of generate-then-verify, we prune DURING the search: at every beam
# expansion a candidate token is kept only if the resulting prefix is still
# structurally completable against the schema graph. The model therefore can
# only ever finish a real table/column -- the same "graph owns form" guarantee
# as data-gen, now enforced token-by-token at inference. Dependency-free (pure
# scan, no sqlglot) so the gate stays fast in the decode loop and fully
# auditable.

_GATE_TOK = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\.|\s+|[^A-Za-z0-9_.\s]+")
_TRAIL_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
# datetime fields that precede FROM inside EXTRACT(YEAR FROM col) -- that FROM
# introduces an expression, NOT a table, so it must not be gated as one.
_EXTRACT_FIELDS = {"YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND"}
# keywords / functions that are never identifiers we gate
_KW = {
    "SELECT", "FROM", "JOIN", "ON", "WHERE", "AND", "OR", "GROUP", "BY", "HAVING",
    "ORDER", "FETCH", "FIRST", "ROWS", "ONLY", "AS", "IN", "ASC", "DESC", "OVER",
    "PARTITION", "EXTRACT", "DISTINCT", "NULL", "IS", "NOT", "LIKE", "BETWEEN",
    "SUM", "AVG", "COUNT", "MIN", "MAX", "RANK", "ROW_NUMBER", "DENSE_RANK",
    "INNER", "LEFT", "RIGHT", "OUTER", "CROSS", "UNION", "ALL",
} | _EXTRACT_FIELDS
# clause keywords that END an ON clause (AND/OR keep it open for multi-key joins)
_CLAUSE_RESET = {"FROM", "JOIN", "WHERE", "GROUP", "HAVING", "ORDER", "FETCH",
                 "SELECT", "UNION"}


class SchemaPrefixGate:
    """Prefix-aware structural gate: is this (possibly partial) SQL still on a
    path to a schema-valid query?

    Returns False ONLY on a definite violation in the prefix:
      * a table position (the bare word right after FROM/JOIN, not a `a.b` ref
        and not EXTRACT's `... FROM col`) that, once complete, is not a real
        table -- or whose still-being-typed partial prefixes no real table;
      * a qualified `alias.column` whose column is not on the alias's table
        (exact once complete, prefix-tolerant while still being typed);
      * an `ON a1.c1 = a2.c2` join key that is not a documented FK edge between
        the two aliased tables (again prefix-tolerant on the right column).

    Everything else is allowed, so every prefix of valid SQL survives (the model
    is free to *complete* a real identifier / FK key) while hallucinated tables,
    columns and join keys are pruned as early as the offending characters appear
    -- PICARD's "reject early" applied to the schema graph rather than full SQL
    grammar.
    """

    def __init__(self, schema):
        self.tables = {t.name.lower() for t in schema.tables}
        self.cols = {
            t.name.lower(): {c.name.lower() for c in t.columns} for t in schema.tables
        }
        # (table1, col1, table2) -> {valid partner col2}, both FK directions.
        self.fk_join: dict[tuple[str, str, str], set[str]] = {}
        for fk in schema.foreign_keys:
            ft, fc = fk.from_table.lower(), fk.from_column.lower()
            tt, tc = fk.to_table.lower(), fk.to_column.lower()
            self.fk_join.setdefault((ft, fc, tt), set()).add(tc)
            self.fk_join.setdefault((tt, tc, ft), set()).add(fc)

    @staticmethod
    def _lex(text: str) -> list[tuple[str, str]]:
        """Tokenize into (kind, text): 'w' word, '.' dot, 'x' other symbols."""
        toks: list[tuple[str, str]] = []
        for m in _GATE_TOK.finditer(text):
            s = m.group()
            if s.isspace():
                continue
            kind = "." if s == "." else "w" if (s[0].isalpha() or s[0] == "_") else "x"
            toks.append((kind, s))
        return toks

    @staticmethod
    def _alias_map(toks) -> dict[str, str]:
        """alias -> table from `FROM/JOIN <table> <alias>` (skip EXTRACT's FROM
        and dotted refs, which never introduce a table)."""
        aliases: dict[str, str] = {}
        for i, (k, s) in enumerate(toks):
            if k != "w" or s.upper() not in ("FROM", "JOIN"):
                continue
            if i > 0 and toks[i - 1][0] == "w" and toks[i - 1][1].upper() in _EXTRACT_FIELDS:
                continue
            if i + 1 < len(toks) and toks[i + 1][0] == "w":
                if i + 2 < len(toks) and toks[i + 2][0] == ".":
                    continue  # `FROM a.col` -> not a table
                tbl = toks[i + 1][1].lower()
                j = i + 2
                if j < len(toks) and toks[j][0] == "w" and toks[j][1].upper() not in _KW:
                    aliases[toks[j][1].lower()] = tbl
        return aliases

    def ok(self, text: str) -> bool:
        toks = self._lex(text)
        if not toks:
            return True
        tail_incomplete = bool(_TRAIL_WORD.search(text))
        last_w = max((i for i, t in enumerate(toks) if t[0] == "w"), default=-1)
        aliases = self._alias_map(toks)

        # pass 2: gate table positions, qualified columns, and ON join keys
        in_on = False
        for i, (k, s) in enumerate(toks):
            if k == "x" and ("(" in s or ")" in s):
                in_on = False                          # a paren ends the ON scope
            if k != "w":
                continue
            u = s.upper()
            if u == "ON":
                in_on = True
            elif u in _CLAUSE_RESET:
                in_on = False

            incomplete = (i == last_w) and tail_incomplete
            prev_is_dot = i > 0 and toks[i - 1][0] == "."
            next_is_dot = i + 1 < len(toks) and toks[i + 1][0] == "."
            prev_fj = (
                i > 0 and toks[i - 1][0] == "w" and toks[i - 1][1].upper() in ("FROM", "JOIN")
                and not (i > 1 and toks[i - 2][0] == "w" and toks[i - 2][1].upper() in _EXTRACT_FIELDS)
            )

            # table position: bare word after FROM/JOIN, not part of `a.b`
            if prev_fj and not next_is_dot and not prev_is_dot and u not in _KW:
                w = s.lower()
                if incomplete:
                    if not any(t.startswith(w) for t in self.tables):
                        return False
                elif w not in self.tables:
                    return False
                continue

            # qualified column: <alias> '.' <col>
            if prev_is_dot and i >= 2 and toks[i - 2][0] == "w":
                q = toks[i - 2][1].lower()
                if q in aliases:                      # unknown alias -> lenient
                    cols = self.cols.get(aliases[q], set())
                    c = s.lower()
                    if incomplete:
                        if not any(col.startswith(c) for col in cols):
                            return False
                    elif c not in cols:
                        return False

                # join key: <a1>.<c1> = <a2>.<c2> inside ON must be a real FK edge
                if (in_on and i >= 6 and toks[i - 3] == ("x", "=")
                        and toks[i - 4][0] == "w" and toks[i - 5][0] == "."
                        and toks[i - 6][0] == "w"):
                    a1, c1 = toks[i - 6][1].lower(), toks[i - 4][1].lower()
                    a2, c2 = q, s.lower()
                    if a1 in aliases and a2 in aliases:
                        valid = self.fk_join.get((aliases[a1], c1, aliases[a2]), ())
                        if incomplete:
                            if not any(cb.startswith(c2) for cb in valid):
                                return False
                        elif c2 not in valid:
                            return False
        return True

    # -- hard constraint: which identifiers can the NEXT token spell? ---------
    def _trailing_slot(self, text: str):
        """If the prefix's tail is a constrained identifier slot, return
        (valid_ids, partial) -- the set of allowed identifiers and the part typed
        so far; else None (the next token is unconstrained).

        Handles both 'just entered the slot' (partial='') and 'mid-identifier'."""
        toks = self._lex(text)
        if not toks:
            return None
        aliases = self._alias_map(toks)
        mid = bool(_TRAIL_WORD.search(text))            # currently typing an identifier

        if mid:
            w = toks[-1][1].lower()
            # table slot: `FROM/JOIN <w`  (not EXTRACT's FROM, not a keyword)
            if (len(toks) >= 2 and toks[-2][0] == "w" and toks[-2][1].upper() in ("FROM", "JOIN")
                    and not (len(toks) >= 3 and toks[-3][1].upper() in _EXTRACT_FIELDS)
                    and w.upper() not in _KW):
                return self.tables, w
            # qualified column: `<alias> . <w`
            if len(toks) >= 3 and toks[-2][0] == "." and toks[-3][0] == "w":
                q = toks[-3][1].lower()
                if q in aliases:
                    return self.cols.get(aliases[q], set()), w
            return None

        # between tokens -> just entered a slot (partial = "")
        last = toks[-1]
        if last[0] == "." and len(toks) >= 2 and toks[-2][0] == "w" \
                and toks[-2][1].lower() in aliases:
            return self.cols.get(aliases[toks[-2][1].lower()], set()), ""
        if last[0] == "w" and last[1].upper() in ("FROM", "JOIN") \
                and not (len(toks) >= 2 and toks[-2][1].upper() in _EXTRACT_FIELDS):
            return self.tables, ""
        return None

    def allowed_next_tokens(self, text: str, tok_strings: dict[int, str]):
        """Token ids the next step may emit. None = unconstrained (any token).
        Otherwise: only tokens that keep the identifier on a path to a real one
        (continue a valid prefix) or, once it's complete, a delimiter."""
        slot = self._trailing_slot(text)
        if slot is None:
            return None
        valid_ids, partial = slot
        suffixes = {vid[len(partial):] for vid in valid_ids if vid.startswith(partial)}
        if not suffixes:
            return set()                                # dead end (off the gold path)
        allow_delim = "" in suffixes                    # identifier may be complete -> delimiter
        conts = [s for s in suffixes if s]              # still-needed continuations
        allowed: set[int] = set()
        for tid, s in tok_strings.items():
            if not s:
                continue
            if s[0].isalnum() or s[0] == "_":           # identifier-fragment token
                if any(suf.startswith(s) for suf in conts):
                    allowed.add(tid)
            elif allow_delim:                            # delimiter token
                allowed.add(tid)
        return allowed


def build_token_strings(tok) -> dict[int, str]:
    """id -> decoded text for every ordinary (non-special) token. Case is kept:
    schema identifiers are lowercase, so matching against them case-sensitively
    rejects uppercase token variants (which would garble the identifier)."""
    return {i: b.decode("utf-8", "replace") for i, b in tok.vocab.items()}


@torch.no_grad()
def masked_beam_search(model, src, src_keep, bos, eos, gate, tok,
                       beam=5, max_len=160, len_penalty=0.7):
    """Beam search with HARD logit masking: at an identifier slot the next token
    is restricted to those that spell a real schema identifier, so the model
    physically cannot emit a hallucinated/garbled name (it picks the most likely
    REAL one). Unconstrained positions decode normally."""
    model.eval()
    device = src.device
    memory = model.encode(src, src_keep)
    tok_strings = build_token_strings(tok)
    beams: list[tuple[list[int], float]] = [([bos], 0.0)]
    finished: list[tuple[list[int], float]] = []

    def norm(item):
        return item[1] / (len(item[0]) ** len_penalty)

    for _ in range(max_len):
        live = [b for b in beams if b[0][-1] != eos]
        finished.extend(b for b in beams if b[0][-1] == eos)
        if not live:
            break
        cands: list[tuple[list[int], float]] = []
        for tokens, score in live:
            tgt = torch.tensor([tokens], device=device)
            h = model.decode(tgt, memory, src_keep, torch.ones_like(tgt, dtype=torch.bool))
            logp = torch.log_softmax(model.lm_head(h[:, -1]), dim=-1)[0]
            allowed = gate.allowed_next_tokens(tok.decode(tokens[1:]), tok_strings)
            if allowed is not None and allowed:         # mask to allowed ids (eos excluded)
                masked = torch.full_like(logp, _NEG)
                idx = torch.tensor(sorted(allowed), device=device)
                masked[idx] = logp[idx]
                logp = masked
            vals, ids = logp.topk(min(beam, logp.numel()))
            for v, i in zip(vals.tolist(), ids.tolist()):
                cands.append((tokens + [i], score + v))
        cands.sort(key=norm, reverse=True)
        beams = cands[:beam]
    finished.extend(beams)
    finished.sort(key=norm, reverse=True)
    return finished


def hard_generate(model, tok, src, src_keep, schema, beam=5, max_len=160):
    """Hard-constrained decode: identifiers are guaranteed real by logit masking.
    Returns (sql, ok) where ok = the result also passes the full graph check
    (join keys included). Identifiers are valid by construction, so there is no
    garbled fallback."""
    bos, eos = tok.special("<bos>"), tok.special("<eos>")
    gate = SchemaPrefixGate(schema)
    graph = SchemaGraph(schema)
    cands = masked_beam_search(model, src, src_keep, bos, eos, gate, tok,
                               beam=beam, max_len=max_len)

    def to_sql(tokens):
        body = tokens[1:]
        if eos in body:
            body = body[: body.index(eos)]
        return tok.decode(body)

    for tokens, _ in cands:
        sql = to_sql(tokens)
        if validate_sqlglot(sql).ok and graph_check_sql(sql, graph).ok:
            return sql, True
    return to_sql(cands[0][0]), False


@torch.no_grad()
def picard_beam_search(model, src, src_keep, bos, eos, gate, tok,
                       beam=5, max_len=160, len_penalty=0.7, expand=8):
    """Beam search with per-step gating: a candidate token survives only if the
    decoded prefix passes `gate.ok` (EOS always allowed). If gating empties a
    beam's expansions we keep its top token so generation never stalls."""
    model.eval()
    device = src.device
    memory = model.encode(src, src_keep)
    beams: list[tuple[list[int], float]] = [([bos], 0.0)]
    finished: list[tuple[list[int], float]] = []
    k = max(beam, expand)

    def norm(item):
        return item[1] / (len(item[0]) ** len_penalty)

    for _ in range(max_len):
        live = [b for b in beams if b[0][-1] != eos]
        finished.extend(b for b in beams if b[0][-1] == eos)
        if not live:
            break
        cands: list[tuple[list[int], float]] = []
        for tokens, score in live:
            tgt = torch.tensor([tokens], device=device)
            h = model.decode(tgt, memory, src_keep, torch.ones_like(tgt, dtype=torch.bool))
            logp = torch.log_softmax(model.lm_head(h[:, -1]), dim=-1)[0]
            vals, idx = logp.topk(k)
            kept: list[tuple[list[int], float]] = []
            for v, i in zip(vals.tolist(), idx.tolist()):
                nxt = tokens + [i]
                if i == eos or gate.ok(tok.decode(nxt[1:])):
                    kept.append((nxt, score + v))
            if not kept:  # gate pruned everything -> don't stall this beam
                i, v = idx[0].item(), vals[0].item()
                kept.append((tokens + [i], score + v))
            cands.extend(kept)
        cands.sort(key=norm, reverse=True)
        beams = cands[:beam]
    finished.extend(beams)
    finished.sort(key=norm, reverse=True)
    return finished


def picard_generate(model, tok, src, src_keep, schema, beam=5, max_len=160):
    """Incremental gated beam search, then pick the best candidate that is fully
    graph-valid (joins included -- the gate covers tables/columns, not FK keys).

    Returns (sql, constrained): constrained=False means even the gated beam
    yielded nothing fully graph-valid, so we returned the top gated candidate.
    """
    bos, eos = tok.special("<bos>"), tok.special("<eos>")
    gate = SchemaPrefixGate(schema)
    graph = SchemaGraph(schema)
    cands = picard_beam_search(model, src, src_keep, bos, eos, gate, tok,
                               beam=beam, max_len=max_len)

    def to_sql(tokens):
        body = tokens[1:]
        if eos in body:
            body = body[: body.index(eos)]
        return tok.decode(body)

    # best-first: first fully graph-valid candidate wins; else the top gated
    # candidate that at least PARSES (gating can steer a doomed beam into
    # malformed SQL, so prefer a parseable one).
    best_parseable = None
    for tokens, _ in cands:
        sql = to_sql(tokens)
        if not validate_sqlglot(sql).ok:
            continue
        if graph_check_sql(sql, graph).ok:
            return sql, True
        if best_parseable is None:
            best_parseable = sql
    if best_parseable is not None:
        return best_parseable, False

    # nothing in the gated beam even parsed (rare, hardest schemas) -> last
    # resort is the UNGATED beam, the same fluent output greedy/verify return.
    for tokens, _ in beam_search(model, src, src_keep, bos, eos, beam=beam, max_len=max_len):
        sql = to_sql(tokens)
        if validate_sqlglot(sql).ok:
            return sql, False
    return to_sql(cands[0][0]), False
