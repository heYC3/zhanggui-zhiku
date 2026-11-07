"""按可追溯证据标识评检索；答案质量需单独复核，不使用答案措辞充当召回标签。"""

from __future__ import annotations

import hashlib


def evidence_id(item_name: str, content: str) -> str:
    return hashlib.sha256((item_name + "\0" + content).encode("utf-8")).hexdigest()


def map_document(doc: dict, corpus: list[dict]) -> str | None:
    text = doc.get("content") or doc.get("text") or ""
    product = doc.get("item_name") or doc.get("product_id")
    matches = [row for row in corpus if row["content"] == text
               and (not product or row["item_name"] == product)]
    ids = {row["evidence_id"] for row in matches}
    return next(iter(ids)) if len(ids) == 1 else None


def score_retrieval(question: dict, docs: list[dict], corpus: list[dict]) -> dict:
    local = [doc for doc in docs if doc.get("source") == "local"]
    mapped = [map_document(doc, corpus) for doc in local]
    points = question["gold_points"]
    gold = {key for point in points for key in point["supporting_chunks"]}
    first = set(mapped[:1]) - {None}
    top3 = set(mapped[:3]) - {None}
    all_ids = set(mapped) - {None}
    covered = [bool(top3.intersection(point["supporting_chunks"])) for point in points]
    covered_all = [bool(all_ids.intersection(point["supporting_chunks"])) for point in points]
    return {
        "hit_at_1": bool(first.intersection(gold)),
        "hit_at_3": bool(top3.intersection(gold)),
        "point_support_at_3": covered,
        "point_support_in_context": covered_all,
        "source_gaps": [i for i, point in enumerate(points) if not point["supporting_chunks"]],
        "retrieved_ids": mapped,
        "unmapped_local_docs": sum(key is None for key in mapped),
        "local_docs_returned": len(local),
    }
