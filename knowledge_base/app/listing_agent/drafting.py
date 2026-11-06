"""用证据原句拼草稿。不调用大模型，避免没有密钥时编造卖点。"""

from __future__ import annotations

import time

from app.listing_agent.catalog import get_product
from app.listing_agent.retrieve import expand_query, terms_of


_GAP_WORDS = ("防水", "降噪", "容量", "续航")


def _lead_chunks(query: str, evidence: list[dict], policy: str) -> list[dict]:
    expanded = expand_query(query)
    bonus = set(terms_of(expanded)) - set(terms_of(query))

    def score(chunk: dict) -> tuple[int, int]:
        text_terms = set(terms_of(chunk["text"]))
        extra = len(text_terms & bonus)
        base = len(text_terms & set(terms_of(query)))
        return (extra * 2 + base, -evidence.index(chunk))

    ordered = sorted(evidence, key=score, reverse=True)
    if policy == "template-draft-fuller":
        return ordered[:5]
    return ordered[:3]


def build_draft(query: str, sku: str, evidence: list[dict], policy: str) -> dict:
    started = time.perf_counter()
    product = get_product(sku)
    if product is None:
        raise LookupError(f"资料里没有型号 {sku}")
    if not evidence:
        raise ValueError("没有证据，不能生成草稿")

    chosen = _lead_chunks(query, evidence, policy)
    lead = chosen[0]["text"].strip()
    title = f"{product['brand']}{product['model']} {lead}".strip()
    if len(title) > 80:
        title = title[:80]
    bullets = []
    for chunk in chosen:
        sentence = chunk["text"].strip()
        if sentence and sentence not in bullets:
            bullets.append(sentence)

    evidence_text = "\n".join(chunk["text"] for chunk in evidence)
    gaps = []
    for word in _GAP_WORDS:
        if word in query and word not in evidence_text:
            gaps.append(f"资料没有写「{word}」，草稿里没有补这一句")

    return {
        "platform": "amazon",
        "sku": sku,
        "brand": product["brand"],
        "model": product["model"],
        "title": title,
        "bullets": bullets,
        "gaps": gaps,
        "evidence_ids": [chunk["id"] for chunk in chosen],
        "policy": policy,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }
