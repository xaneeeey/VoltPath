"""Maintenance knowledge base for copilot retrieval (pgvector).

Embeddings use the hashing trick over word unigrams + bigrams (256-d, signed, sublinear TF,
L2-normalised): deterministic, offline, no model download. It is lexical rather than semantic -
swapping in a real embedding model only changes `embed()` and the vector dimension (ADR-0003).
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

import numpy as np

from voltpath.config import ROOT

DIM = 256
KB_DIR = ROOT / "data" / "kb"
TOKEN = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "of", "to", "and", "or", "is", "in", "on", "for", "with", "be", "it", "at", "by", "as",
        "this", "that", "are", "if", "when", "what", "how", "do", "does", "i", "my", "we"}


def embed(text: str) -> np.ndarray:
    toks = [t for t in TOKEN.findall(text.lower()) if t not in STOP]
    feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]
    counts: dict[int, float] = {}
    for f in feats:
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
        idx, sign = h % DIM, (1.0 if (h >> 63) & 1 else -1.0)
        counts[idx] = counts.get(idx, 0.0) + sign
    v = np.zeros(DIM, dtype=np.float32)
    for i, c in counts.items():
        v[i] = math.copysign(1 + math.log(abs(c)), c) if c else 0.0
    n = np.linalg.norm(v)
    return v / n if n else v


def vec_literal(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


def chunks() -> list[tuple[str, str]]:
    out = []
    for f in sorted(Path(KB_DIR).glob("*.md")):
        text = f.read_text()
        for sec in re.split(r"\n(?=## )", text):
            lines = sec.strip().splitlines()
            if not lines:
                continue
            title = lines[0].lstrip("# ").strip()
            body = " ".join(line.strip() for line in lines[1:] if line.strip())
            if body:
                out.append((title, body))
    return out


def load_kb(url: str | None = None) -> int:
    import psycopg

    from voltpath.config import settings

    items = chunks()
    with psycopg.connect(url or settings().database_url) as c:
        c.execute("TRUNCATE kb_chunk RESTART IDENTITY")
        for title, body in items:
            c.execute("INSERT INTO kb_chunk(title, body, embedding) VALUES (%s, %s, %s::vector)",
                      (title, body, vec_literal(embed(title + " " + body))))
    return len(items)


async def search_kb(conn, query: str, k: int = 3) -> list[dict]:
    """Hybrid retrieval: exact DTC-code matches first (lexical, precise), then vector k-NN."""
    from voltcore.dtc import parse_dtcs

    exact = []
    codes = parse_dtcs(query)
    if codes:
        exact = await (await conn.execute(
            "SELECT title, body, 1.0 AS score FROM kb_chunk WHERE title ILIKE ANY(%s) LIMIT %s",
            ([f"%{c}%" for c in codes], k))).fetchall()
    rows = await (await conn.execute(
        "SELECT title, body, round((1 - (embedding <=> %s::vector))::numeric, 3) AS score FROM kb_chunk "
        "ORDER BY embedding <=> %s::vector LIMIT %s", (vec_literal(embed(query)),) * 2 + (k,))).fetchall()
    seen = {r["title"] for r in exact}
    return (exact + [r for r in rows if r["title"] not in seen])[:k]
