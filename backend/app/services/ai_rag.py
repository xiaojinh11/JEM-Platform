"""RAG layer: HyDE query expansion + multi-recall fusion + refusal gate.

Three concerns, one module:

1. **HyDE** (Hypothetical Document Embeddings) — when the user asks
   "how do I fix X?", the literal query often has zero keyword overlap
   with the manual.  We ask the LLM to *imagine* a manual section that
   would answer the question, then BM25-search that hypothetical
   passage.  Recall jumps dramatically for procedural / descriptive
   queries at the cost of one extra cheap LLM call.

2. **Multi-recall fusion** — combine hits from the original query,
   the HyDE passage, and (optionally) a dense embedding model via
   Reciprocal Rank Fusion.  When ``settings.rag_dense_enabled`` is
   False (default — sandbox cannot download embedding models), the
   dense leg is silently skipped and BM25 carries the load.

3. **Refusal gate** — if the top-hit BM25 score is below a tuned
   threshold we return ``no_recall=True`` so the caller (system
   prompt / tool wrapper) can refuse rather than hallucinate.

The module is a thin orchestration layer; the heavy lifting lives in
``documents.search`` (BM25) and (when enabled) ``_dense_embed``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings


# ── Configuration (lazy from settings) ────────────────────────────────

def _min_score() -> float:
    """Reject hits below this BM25 score (treat as no-recall)."""
    return float(getattr(settings, "rag_min_score", 1.0))


def _hyde_max_tokens() -> int:
    """Cap the HyDE passage so a runaway LLM cannot blow context."""
    return int(getattr(settings, "rag_hyde_max_tokens", 220))


def _dense_enabled() -> bool:
    """Dense embedding leg — OFF by default (no model bundled)."""
    return bool(getattr(settings, "rag_dense_enabled", False))


def _rrf_k() -> int:
    """Reciprocal Rank Fusion constant — standard k=60 from Cormack et al."""
    return int(getattr(settings, "rag_rrf_k", 60))


# ── Public data types ─────────────────────────────────────────────────


@dataclass
class RecallHit:
    """One merged retrieval result with provenance."""

    id: int
    title: str
    vendor: str | None
    category: str | None
    snippet: str
    score: float  # fused RRF score (higher = more relevant)
    sources: list[str] = field(default_factory=list)  # which legs hit this doc


@dataclass
class RagResult:
    """Outcome of a RAG query."""

    query: str
    hyde_passage: str | None
    hits: list[RecallHit]
    recall_paths: list[str]
    no_recall: bool
    note: str | None = None

    def to_tool_dict(self, limit: int = 5) -> dict[str, Any]:
        """Format for the AI tool wrapper."""
        if self.no_recall:
            return {
                "count": 0,
                "documents": [],
                "no_recall": True,
                "note": self.note or "知识库无相关内容。",
            }
        top = self.hits[:limit]
        return {
            "count": len(top),
            "documents": [
                {
                    "id": h.id,
                    "title": h.title,
                    "vendor": h.vendor or "",
                    "category": h.category or "",
                    "snippet": h.snippet,
                    "score": round(h.score, 4),
                    "sources": h.sources,
                }
                for h in top
            ],
            "recall_paths": self.recall_paths,
        }


# ── HyDE passage generation ──────────────────────────────────────────


_HYDE_SYSTEM = (
    "你是一名网络设备运维工程师。基于用户的问题，"
    "**想象**一段会出现在设备手册中的章节内容（200字以内），"
    "包含回答该问题所需的关键命令、参数或概念。\n"
    "要求：\n"
    "1. 不要编造具体型号/版本号，使用通用术语\n"
    "2. 用中文回答，风格像手册正文\n"
    "3. 只输出段落正文，不要任何解释或前缀"
)


async def generate_hyde_passage(user_query: str, *, cfg: Any, call_llm: Any) -> str | None:
    """Ask the LLM to hallucinate a manual passage matching the query.

    Returns ``None`` on any failure (network / parse / empty) so the
    caller can fall back to plain BM25 — HyDE is strictly an enhancer.
    """
    try:
        response = await call_llm(
            cfg,
            messages=[{"role": "system", "content": _HYDE_SYSTEM},
                       {"role": "user", "content": user_query}],
            max_tokens=_hyde_max_tokens(),
            temperature=0.4,
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("HyDE generation failed: %s", exc)
        return None

    if not response:
        return None
    # Strip leading whitespace and any "以下是..." prefix the model may add.
    text = response.strip()
    return text or None


# ── BM25 leg ──────────────────────────────────────────────────────────


def _bm25_search(query: str, limit: int) -> list[dict]:
    """Wrapper over the existing Whoosh BM25 search."""
    from app.services.documents import search as whoosh_search

    try:
        return whoosh_search(query, limit=max(limit, 1))
    except Exception as exc:  # noqa: BLE001
        logging.warning("BM25 search failed for %r: %s", query[:40], exc)
        return []


# ── Dense leg (optional) ──────────────────────────────────────────────


_DENSE_MODEL = None  # lazy singleton


def _get_dense_model():
    """Lazy-load the embedding model once per process.

    Returns ``None`` if dense is disabled or the model fails to load
    (network missing, model file absent, etc.) — callers must handle
    ``None`` as "dense leg unavailable, fall back to BM25 only".
    """
    global _DENSE_MODEL
    if not _dense_enabled():
        return None
    if _DENSE_MODEL is not None:
        return _DENSE_MODEL
    try:
        from fastembed import TextEmbedding  # type: ignore
        # bge-small: 384-dim, ~33 MB, runs on CPU in <30ms per chunk
        _DENSE_MODEL = TextEmbedding("BAAI/bge-small-en-v1.5")
        return _DENSE_MODEL
    except Exception as exc:  # noqa: BLE001
        logging.warning("Dense embedding model unavailable: %s", exc)
        return None


def _dense_search(query: str, limit: int) -> list[dict]:
    """Vector-search the document corpus using a local embedding model.

    NOTE: Until a vector index (Qdrant / pgvector / numpy store) is
    wired in, we fall back to embedding all document snippets on the
    fly — only useful for tiny corpora (<200 docs).  For larger
    corpora this function should be replaced with a proper ANN search.
    Returns ``[]`` when dense is disabled or no model is loadable.
    """
    model = _get_dense_model()
    if model is None:
        return []
    # Vector store plumbing lives elsewhere; for now this is a stub
    # that returns nothing rather than burning minutes embedding every
    # doc per query.  The hook is here for the next iteration.
    return []


# ── Reciprocal Rank Fusion ──────────────────────────────────────────


def _rrf_merge(
    *ranked_lists: list[dict],
    k: int,
) -> list[tuple[dict, float, list[str]]]:
    """Reciprocal Rank Fusion across multiple ranked lists.

    Each input list is ``[doc_dict, ...]`` ordered best→worst.
    Returns ``[(doc_dict, fused_score, sources), ...]`` sorted by
    fused_score desc.  Ties broken by first-appearance order.
    """
    scores: dict[int, float] = {}
    doc_by_id: dict[int, dict] = {}
    sources: dict[int, list[str]] = {}

    for list_name, items in ranked_lists:
        for rank, doc in enumerate(items, start=1):
            doc_id = doc["id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            doc_by_id[doc_id] = doc
            if list_name not in sources.setdefault(doc_id, []):
                sources[doc_id].append(list_name)

    merged = [
        (doc_by_id[doc_id], scores[doc_id], sources[doc_id])
        for doc_id in sorted(scores, key=lambda i: scores[i], reverse=True)
    ]
    return merged


# ── Top-level entry point ────────────────────────────────────────────


async def rag_search(
    *,
    user_query: str,
    cfg: Any,
    call_llm: Any,
    limit: int = 5,
) -> RagResult:
    """Full RAG pipeline: HyDE → BM25 → (optional) dense → RRF → refuse.

    Args:
        user_query: the user's literal question.
        cfg: AIConfig — passed to ``call_llm`` for the HyDE call.
        call_llm: async callable ``(cfg, messages, max_tokens, temperature) → str``.
        limit: max hits to return.

    The result is a :class:`RagResult`; the tool wrapper turns it into
    JSON the LLM can read.
    """
    recall_paths: list[str] = ["BM25"]

    # Leg 1: plain BM25 on the user query
    raw_bm25 = _bm25_search(user_query, limit=limit * 3)

    # Leg 2: HyDE — hallucinated passage
    hyde = await generate_hyde_passage(user_query, cfg=cfg, call_llm=call_llm)
    raw_hyde: list[dict] = []
    if hyde:
        raw_hyde = _bm25_search(hyde, limit=limit * 3)
        if raw_hyde:
            recall_paths.append("HyDE→BM25")

    # Leg 3: dense (optional)
    raw_dense: list[dict] = []
    if _dense_enabled():
        raw_dense = _dense_search(user_query, limit=limit * 3)
        if raw_dense:
            recall_paths.append("dense")

    # RRF merge
    merged = _rrf_merge(
        ("bm25", raw_bm25),
        ("hyde", raw_hyde),
        ("dense", raw_dense),
        k=_rrf_k(),
    )

    # Refusal gate
    min_score = _min_score()
    if not merged:
        return RagResult(
            query=user_query,
            hyde_passage=hyde,
            hits=[],
            recall_paths=recall_paths,
            no_recall=True,
            note="知识库中没有匹配的文档。请换更具体的关键词，或去「手册库」手动浏览。",
        )

    # Use the original BM25 score (not the fused RRF score) for the
    # threshold check — RRF scores are unitless and would need a
    # separate calibration.  Fused score is still useful for ranking.
    top_doc, top_score, top_sources = merged[0]
    raw_top_score = top_doc.get("score", 0.0)
    if raw_top_score < min_score:
        return RagResult(
            query=user_query,
            hyde_passage=hyde,
            hits=[],
            recall_paths=recall_paths,
            no_recall=True,
            note=(
                f"检索召回分数过低（top score={raw_top_score:.2f} < {min_score:.1f}），"
                "知识库可能没有覆盖该问题。建议：换更具体的关键词（厂商名、命令关键字、错误码）重试。"
            ),
        )

    hits = [
        RecallHit(
            id=doc["id"],
            title=doc.get("title", ""),
            vendor=doc.get("vendor"),
            category=doc.get("category"),
            snippet=doc.get("snippet", ""),
            score=fused,
            sources=sources,
        )
        for doc, fused, sources in merged
        if doc.get("score", 0.0) >= min_score * 0.5  # keep near-threshold hits for context
    ]

    return RagResult(
        query=user_query,
        hyde_passage=hyde,
        hits=hits,
        recall_paths=recall_paths,
        no_recall=False,
    )
