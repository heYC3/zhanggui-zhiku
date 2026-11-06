"""自编商品资料。这些不是在售商品。"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from app.listing_agent.paths import PRODUCT_DIR


def load_catalog() -> list[dict]:
    products = []
    for path in sorted(PRODUCT_DIR.glob("*.json")):
        product = json.loads(path.read_text(encoding="utf-8"))
        product["source_file"] = path.name
        products.append(product)
    if not products:
        raise FileNotFoundError(f"没有找到商品资料：{PRODUCT_DIR}")
    return products


def get_product(sku: str) -> dict | None:
    for product in load_catalog():
        if product["sku"] == sku:
            return product
    return None


def display_name(product: dict) -> str:
    return f"{product['brand']}{product['model']}"


def contains_alias(text: str, alias: str) -> bool:
    """型号别名要按整词匹配，避免 X1 误伤 X1 Pro 的边界判断。"""
    if not text or not alias:
        return False
    if re.search(r"[\u4e00-\u9fff]", alias):
        return alias.lower() in text.lower()
    pattern = rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


_REF_WORDS = ("它", "这个", "这款", "刚才", "上面那个")


def _unique_hits(text: str, catalog: list[dict]) -> list[tuple[int, str]]:
    hits = []
    for product in catalog:
        for alias in product.get("unique_aliases") or []:
            if contains_alias(text, alias):
                hits.append((len(alias), product["sku"]))
    return hits


def _shared_hits(text: str, catalog: list[dict]) -> list[tuple[int, str]]:
    hits = []
    for product in catalog:
        for alias in product.get("shared_aliases") or []:
            if contains_alias(text, alias):
                hits.append((len(alias), product["sku"]))
    return hits


def _pick(hits: list[tuple[int, str]]) -> list[str]:
    if not hits:
        return []
    best = max(length for length, _sku in hits)
    skus = []
    for length, sku in hits:
        if length == best and sku not in skus:
            skus.append(sku)
    return skus


def resolve_product(query: str, history: list[str] | None = None) -> dict:
    """从当前这句话和之前几句话里确定型号。

    返回 kind：one / many / none。
    many 表示型号有歧义，调用方应先问人，不能直接写草稿。
    """
    catalog = load_catalog()
    history = [item for item in (history or []) if item]

    unique = _pick(_unique_hits(query, catalog))
    if len(unique) == 1:
        return {"kind": "one", "sku": unique[0], "candidates": unique, "via": "query"}
    if len(unique) > 1:
        return {"kind": "many", "sku": "", "candidates": unique, "via": "query"}

    referential = any(word in query for word in _REF_WORDS)
    if referential and history:
        remembered = _pick(_unique_hits("\n".join(history), catalog))
        if len(remembered) == 1:
            return {
                "kind": "one",
                "sku": remembered[0],
                "candidates": remembered,
                "via": "history",
            }
        if len(remembered) > 1:
            return {
                "kind": "many",
                "sku": "",
                "candidates": remembered,
                "via": "history",
            }

    shared = _pick(_shared_hits(query, catalog))
    if len(shared) == 1:
        return {"kind": "one", "sku": shared[0], "candidates": shared, "via": "family"}
    if len(shared) > 1:
        return {"kind": "many", "sku": "", "candidates": shared, "via": "family"}
    return {"kind": "none", "sku": "", "candidates": [], "via": "none"}


def chunks_for(sku: str | None = None) -> list[dict]:
    rows = []
    for product in load_catalog():
        if sku and product["sku"] != sku:
            continue
        for chunk in product["chunks"]:
            rows.append(
                {
                    "id": chunk["id"],
                    "title": chunk["title"],
                    "text": chunk["text"],
                    "sku": product["sku"],
                    "brand": product["brand"],
                    "model": product["model"],
                }
            )
    return rows


@lru_cache(maxsize=1)
def evidence_text_by_sku() -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for chunk in chunks_for():
        grouped.setdefault(chunk["sku"], []).append(chunk["text"])
    return {sku: "\n".join(parts) for sku, parts in grouped.items()}
