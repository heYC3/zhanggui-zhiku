"""问答和 Listing 共用的证据接口。两条工作流仍然分开。"""

from __future__ import annotations

import os
import json

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
    return _from_milvus(query, resolved, product_id, limit, strategy=strategy)


def as_query_hit(row: dict) -> dict:
    """问答融合节点仍读 entity。字段来自上面的同一条证据。"""
    return {
        "entity": {
            "chunk_id": row.get("chunk_id") or "",
            "content": row.get("text") or "",
            "item_name": row.get("product_id") or row.get("sku") or "",
            "title": row.get("title") or "",
            "source": row.get("source") or "",
        }
    }


def retrieve_products_evidence(query: str, product_ids: list[str], *,
                              strategy: str, limit_per_product: int = 12) -> list[dict]:
    """比较问题保留每个已确认产品的召回额度；任一路失败向上传递。"""
    evidence = []
    seen = set()
    from app.evidence.identity import topic_query
    query = topic_query(query, product_ids)
    for product_id in dict.fromkeys(product_ids):
        found = retrieve_product_evidence(query, product_id=product_id, strategy=strategy,
                                         limit=limit_per_product)
        for row in found["evidence"]:
            key = (row.get("product_id"), row.get("chunk_id"))
            if key not in seen:
                evidence.append(row)
                seen.add(key)
    return evidence


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


def _from_milvus(query: str, sku: str | None, product_id: str | None, limit: int, *, strategy: str) -> dict:
    from app.clients.milvus_utils import create_hybrid_search_requests, get_milvus_client, hybrid_search
    from app.conf.milvus_config import milvus_config
    from app.lm.embedding_utils import generate_embeddings

    embeddings = generate_embeddings([query])
    dense_vec = embeddings.get("dense")[0]
    sparse_vec = embeddings.get("sparse")[0]
    names = [name for name in (product_id, sku) if name]
    expr = None
    if names:
        expr = "item_name in " + json.dumps(names, ensure_ascii=False)
    collection = milvus_config.chunks_collection
    reqs = create_hybrid_search_requests(
        dense_vector=dense_vec,
        sparse_vector=sparse_vec,
        expr=expr,
        limit=max(limit * 2, 12),
    )
    client = get_milvus_client()
    if client is None:
        raise RuntimeError("向量库不可用")
    res = hybrid_search(
        client=client,
        collection_name=collection,
        reqs=reqs,
        ranker_weights=(0.8, 0.2),
        norm_score=True,
        limit=limit,
        output_fields=["chunk_id", "content", "item_name", "title", "file_title"],
    )
    if res is None:
        raise RuntimeError("向量检索执行失败")
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
                "title": entity.get("title") or "",
                "source": f"milvus:{collection}#{chunk_id}",
                "sku": sku or "",
                "product_id": entity.get("item_name") or product_id or sku or "",
                "score": hit.get("distance"),
            }
        )
    # 只让原始问题的分支补充词语召回，HyDE 的猜测不能进入词语检索。
    if product_id and strategy == "rrf":
        from app.evidence.manual_retrieval import candidate_union, lexical_rank
        rows = _product_chunks(client, collection, expr)
        lexical = []
        for row in lexical_rank(query, rows, limit=max(limit, 12)):
            cid = str(row["chunk_id"])
            lexical.append({"id": cid, "chunk_id": cid, "text": row.get("content") or "",
                            "title": row.get("title") or "", "source": f"milvus:{collection}#{cid}",
                            "sku": sku or "", "product_id": row.get("item_name") or product_id,
                            "score": None})
        evidence = candidate_union([evidence, lexical], limit)
    return {
        "evidence": evidence,
        "meta": {"backend": "milvus", "strategy": "milvus-hybrid-lexical" if strategy == "rrf" else "milvus-hybrid"},
        "backend": "milvus",
    }


def _product_chunks(client, collection: str, expr: str) -> list[dict]:
    """只读取已经确认的产品范围；分页取全，失败或超限明确报错。"""
    if not expr:
        raise ValueError("词语补充召回必须指定产品范围")
    rows = []
    batch_size = 256
    for offset in range(0, 16384, batch_size):
        batch = client.query(collection_name=collection, filter=expr, limit=batch_size, offset=offset,
                             output_fields=["chunk_id", "content", "item_name", "title"])
        rows.extend(batch)
        if len(batch) < batch_size:
            return rows
    raise RuntimeError("当前产品正文超过分页读取上限，不能截断后当作完整词语索引")
