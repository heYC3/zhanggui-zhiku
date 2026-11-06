"""商品资料检索。

默认用离线词块，保证没有显卡、没有 Milvus 也能复现。
RRF 的算法和问答检索一样：得分加上 1 / (60 + 名次)。
LISTING_USE_BGE=1 时，稠密和稀疏两路改走本仓库的 BGE-M3。
"""

from __future__ import annotations

import hashlib
import os
import re
import time

from app.listing_agent.catalog import chunks_for


# 只在问法换成了口语时补几个同义说法。这是模板 HyDE，不调用大模型。
_HYDE_HINTS = (
    ("能听", "连续播放"),
    ("听多久", "连续播放 小时"),
    ("续航", "连续播放 小时"),
    ("降噪", "主动降噪"),
)

_RRF_K = 60


def expand_query(query: str) -> str:
    extras = [hint for needle, hint in _HYDE_HINTS if needle in query]
    if not extras:
        return query
    return query + "\n" + " ".join(extras)


def terms_of(text: str) -> list[str]:
    """中文按 2 到 4 个字切，英文和数字按整词切。"""
    lowered = (text or "").lower()
    words = re.findall(r"[a-z0-9]+", lowered)
    grams: list[str] = []
    for segment in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(segment) <= 4:
            grams.append(segment)
        for size in (2, 3, 4):
            for index in range(0, len(segment) - size + 1):
                grams.append(segment[index : index + size])
    return words + grams


def _overlap(query: str, text: str) -> int:
    left = set(terms_of(query))
    right = set(terms_of(text))
    if not left or not right:
        return 0
    return len(left & right)


def _stable_bucket(term: str, dim: int) -> int:
    digest = hashlib.md5(term.encode("utf-8")).hexdigest()
    return int(digest, 16) % dim


def _hash_vector(text: str, dim: int = 64) -> list[float]:
    vector = [0.0] * dim
    for term in set(terms_of(text)):
        vector[_stable_bucket(term, dim)] += 1.0
    return vector


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _rank_by_score(chunks: list[dict], score_of) -> list[dict]:
    scored = []
    for chunk in chunks:
        score = float(score_of(chunk))
        if score <= 0:
            continue
        row = dict(chunk)
        row["score"] = round(score, 6)
        scored.append(row)
    scored.sort(key=lambda item: (-item["score"], item["id"]))
    return scored


def _lexical(query: str, chunks: list[dict]) -> list[dict]:
    return _rank_by_score(chunks, lambda chunk: _overlap(query, chunk["text"]))


def _hashed_dense(query: str, chunks: list[dict]) -> list[dict]:
    query_vector = _hash_vector(query)
    return _rank_by_score(
        chunks, lambda chunk: _cosine(query_vector, _hash_vector(chunk["text"]))
    )


def _sparse_dot(left: dict, right: dict) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


def _bge_channels(query: str, chunks: list[dict]) -> tuple[list[dict], list[dict]]:
    """调用本仓库的 BGE-M3。调用方负责在失败时降级。"""
    from app.lm.embedding_utils import generate_embeddings

    vectors = generate_embeddings([query] + [chunk["text"] for chunk in chunks])
    query_dense = vectors["dense"][0]
    query_sparse = vectors["sparse"][0]
    dense_rows = []
    sparse_rows = []
    for index, chunk in enumerate(chunks):
        dense_score = _cosine(query_dense, vectors["dense"][index + 1])
        sparse_score = _sparse_dot(query_sparse, vectors["sparse"][index + 1])
        if dense_score > 0:
            row = dict(chunk)
            row["score"] = round(float(dense_score), 6)
            dense_rows.append(row)
        if sparse_score > 0:
            row = dict(chunk)
            row["score"] = round(float(sparse_score), 6)
            sparse_rows.append(row)
    dense_rows.sort(key=lambda item: (-item["score"], item["id"]))
    sparse_rows.sort(key=lambda item: (-item["score"], item["id"]))
    return dense_rows, sparse_rows


def _rrf(channels: list[list[dict]], limit: int = 3) -> list[dict]:
    scores: dict[str, float] = {}
    payloads: dict[str, dict] = {}
    for channel in channels:
        for rank, chunk in enumerate(channel, start=1):
            scores[chunk["id"]] = scores.get(chunk["id"], 0.0) + 1.0 / (_RRF_K + rank)
            payloads.setdefault(chunk["id"], chunk)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    rows = []
    for chunk_id in ordered[:limit]:
        row = dict(payloads[chunk_id])
        row["score"] = round(scores[chunk_id], 6)
        rows.append(row)
    return rows


def _rerank(query: str, rows: list[dict], mode: str) -> tuple[list[dict], dict]:
    info = {"reranker": "local-term-overlap", "degraded": False, "error": ""}
    if mode == "off":
        info["reranker"] = "off"
        return rows, info
    if mode == "down":
        info["degraded"] = True
        info["reranker"] = "degraded_to_rrf"
        info["error"] = "重排序服务不可用，已保留融合排序"
        return rows, info
    try:
        if mode == "boom":
            raise RuntimeError("重排序服务不可用")
        ranked = sorted(
            rows,
            key=lambda row: (-_overlap(query, row["text"]), row["id"]),
        )
        return ranked, info
    except Exception as exc:  # 工具失败要留下降级说明，不能假装精排成功
        info["degraded"] = True
        info["reranker"] = "degraded_to_rrf"
        info["error"] = str(exc)
        return rows, info


def search_evidence(
    query: str,
    sku: str | None = None,
    strategy: str = "rrf_hyde",
    rerank_mode: str = "on",
    limit: int = 3,
) -> dict:
    """按策略检索证据。strategy 取 sparse / dense / rrf / rrf_hyde。"""
    started = time.perf_counter()
    chunks = chunks_for(sku)
    use_bge = os.getenv("LISTING_USE_BGE", "").strip() in {"1", "true", "yes"}
    embedding_name = "offline-bigram-64"
    bge_error = ""
    dense_rows: list[dict] = []
    sparse_rows: list[dict] = []

    if use_bge:
        try:
            dense_rows, sparse_rows = _bge_channels(query, chunks)
            embedding_name = "BAAI/bge-m3"
        except Exception as exc:
            bge_error = str(exc)
            use_bge = False

    if not use_bge:
        sparse_rows = _lexical(query, chunks)
        dense_rows = _hashed_dense(query, chunks)
        embedding_name = "offline-bigram-64"

    hyde_query = expand_query(query)
    hyde_rows = _lexical(hyde_query, chunks) if hyde_query != query else []

    if strategy == "sparse":
        fused = sparse_rows[:limit]
    elif strategy == "dense":
        fused = dense_rows[:limit]
    elif strategy == "rrf":
        fused = _rrf([sparse_rows, dense_rows], limit=limit)
    elif strategy == "rrf_hyde":
        fused = _rrf([sparse_rows, dense_rows, hyde_rows], limit=limit)
    else:
        raise ValueError(f"不认识的检索策略：{strategy}")

    ranked, rerank_info = _rerank(query, fused, rerank_mode)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "evidence": ranked,
        "meta": {
            "strategy": strategy,
            "embedding": embedding_name,
            "hyde_query": hyde_query,
            "hyde_used": hyde_query != query,
            "reranker": rerank_info["reranker"],
            "rerank_degraded": rerank_info["degraded"],
            "rerank_error": rerank_info["error"],
            "bge_error": bge_error,
            "duration_ms": elapsed_ms,
            "candidate_counts": {
                "sparse": len(sparse_rows),
                "dense": len(dense_rows),
                "hyde": len(hyde_rows),
            },
        },
    }
