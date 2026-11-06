"""问答和 Listing 共用的证据接口。两条工作流仍然分开。"""

from __future__ import annotations

import os

from app.listing_agent.catalog import display_name, get_product, load_catalog
from app.listing_agent.retrieve import search_evidence


def sku_for_product_id(product_id: str | None, sku: str | None = None) -> str | None:
    if sku and get_product(sku):
        return sku
    if not product_id:
        return None
    if get_product(product_id):
        return product_id
    for product in load_catalog():
        names = {
            product["sku"],
            display_name(product),
            f"{product['brand']} {product['model']}",
            *(product.get("unique_aliases") or []),
        }
        if product_id in names:
            return product["sku"]
    return None


def retrieve_product_evidence(
    query: str,
    *,
    sku: str | None = None,
    product_id: str | None = None,
    strategy: str = "rrf_hyde",
    rerank_mode: str = "on",
    limit: int = 3,
) -> dict:
    """返回原句、chunk_id 和来源。目录里有的商品走离线资料，其余再走手册向量库。"""
    resolved = sku_for_product_id(product_id, sku)
    forced = os.getenv("EVIDENCE_BACKEND", "").strip().lower()
    if resolved and forced != "milvus":
        return _from_catalog(query, resolved, strategy, rerank_mode, limit)
    if forced == "offline":
        return {"evidence": [], "meta": {"backend": "offline", "strategy": strategy}, "backend": "offline"}
    return _from_milvus(query, resolved, product_id, limit)


def as_query_hit(row: dict) -> dict:
    """问答融合节点仍读 entity。字段来自上面的同一条证据。"""
    return {
        "entity": {
            "chunk_id": row.get("chunk_id") or "",
            "content": row.get("text") or "",
            "item_name": row.get("product_id") or row.get("sku") or "",
            "source": row.get("source") or "",
        }
    }


def _from_catalog(query: str, sku: str, strategy: str, rerank_mode: str, limit: int) -> dict:
    found = search_evidence(query, sku=sku, strategy=strategy, rerank_mode=rerank_mode, limit=limit)
    evidence = []
    for row in found["evidence"]:
        chunk_id = row["id"]
        evidence.append(
            {
                "id": chunk_id,
                "chunk_id": chunk_id,
                "text": row.get("text") or "",
                "title": row.get("title") or "",
                "source": f"listing_data/products/{sku}.json#{chunk_id}",
                "sku": sku,
                "product_id": sku,
                "score": row.get("score"),
            }
        )
    meta = dict(found["meta"])
    meta["backend"] = "offline"
    return {"evidence": evidence, "meta": meta, "backend": "offline"}


def _from_milvus(query: str, sku: str | None, product_id: str | None, limit: int) -> dict:
    from app.clients.milvus_utils import create_hybrid_search_requests, get_milvus_client, hybrid_search
    from app.conf.milvus_config import milvus_config
    from app.lm.embedding_utils import generate_embeddings

    embeddings = generate_embeddings([query])
    dense_vec = embeddings.get("dense")[0]
    sparse_vec = embeddings.get("sparse")[0]
    names = [name for name in (product_id, sku) if name]
    expr = None
    if names:
        quoted = ", ".join(f'"{name}"' for name in names)
        expr = f"item_name in [{quoted}]"
    collection = milvus_config.chunks_collection
    reqs = create_hybrid_search_requests(
        dense_vector=dense_vec,
        sparse_vector=sparse_vec,
        expr=expr,
        limit=max(limit, 5),
    )
    res = hybrid_search(
        client=get_milvus_client(),
        collection_name=collection,
        reqs=reqs,
        ranker_weights=(0.8, 0.2),
        norm_score=True,
        limit=limit,
        output_fields=["chunk_id", "content", "item_name"],
    )
    hits = res[0] if res else []
    evidence = []
    for hit in hits:
        entity = hit.get("entity") or hit
        chunk_id = str(entity.get("chunk_id") or "")
        evidence.append(
            {
                "id": chunk_id,
                "chunk_id": chunk_id,
                "text": entity.get("content") or "",
                "title": "",
                "source": f"milvus:{collection}#{chunk_id}",
                "sku": sku or "",
                "product_id": entity.get("item_name") or product_id or sku or "",
                "score": hit.get("distance"),
            }
        )
    return {
        "evidence": evidence,
        "meta": {"backend": "milvus", "strategy": "milvus-hybrid"},
        "backend": "milvus",
    }
