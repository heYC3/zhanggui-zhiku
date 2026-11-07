"""手册名称的确定性对齐；别名来自资料目录，不包含问答或评测答案。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


def load_manual_catalog() -> list[dict]:
    path = os.getenv("MANUAL_CATALOG_PATH", "").strip()
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = data.get("products", data) if isinstance(data, dict) else data
        if not isinstance(rows, list) or any(not row.get("item_name") for row in rows):
            raise ValueError("手册目录必须包含 item_name")
        return rows
    # 没有私有目录时，使用库里已有的名称和文件标题；不靠语义分数猜型号。
    from app.clients.milvus_utils import get_milvus_client
    from app.conf.milvus_config import milvus_config

    client = get_milvus_client()
    if client is None:
        raise RuntimeError("手册名称库不可用")
    collection = milvus_config.item_name_collection
    client.load_collection(collection)
    rows = client.query(collection_name=collection, filter="", limit=16384,
                        output_fields=["item_name", "file_title"])
    names: dict[str, set[str]] = {}
    for row in rows:
        name = row.get("item_name") or ""
        if not name:
            continue
        aliases = names.setdefault(name, {name})
        if row.get("file_title"):
            aliases.add(row["file_title"])
        aliases.update(re.findall(r"(?<![A-Za-z0-9])[A-Za-z]+[A-Za-z0-9-]*\d[A-Za-z0-9-]*", name))
    return [{"item_name": name, "aliases": sorted(aliases)} for name, aliases in names.items()]


def match_product_names(text: str, catalog: list[dict]) -> tuple[list[str], bool]:
    """最长别名优先；同一别名属于多个产品时返回歧义，不按顺序挑一个。"""
    found = []
    for row in catalog:
        for alias in {row["item_name"], *(row.get("aliases") or [])}:
            if not alias:
                continue
            pattern = re.escape(alias).replace(r"\ ", r"\s*")
            # X1 不能误匹配 X10、X1Pro 或尚未登记的 X1 Pro。
            tail = r"(?![A-Za-z0-9]|\s+(?:pro|plus|max|ultra)\b)" if alias[-1].isascii() and alias[-1].isalnum() else ""
            head = r"(?<![A-Za-z0-9])" if alias[0].isascii() and alias[0].isalnum() else ""
            for match in re.finditer(head + pattern + tail, text, flags=re.IGNORECASE):
                found.append((match.start(), match.end(), row["item_name"]))
    kept = [hit for hit in found if not any(other[0] <= hit[0] and other[1] >= hit[1]
             and other[1] - other[0] > hit[1] - hit[0] for other in found)]
    spans: dict[tuple[int, int], set[str]] = {}
    for start, end, name in kept:
        spans.setdefault((start, end), set()).add(name)
    ambiguous = any(len(names) > 1 for names in spans.values())
    return list(dict.fromkeys(name for _, _, name in sorted(kept))), ambiguous


def resolve_product_context(query: str, history: list[dict], catalog: list[dict]) -> dict:
    names, ambiguous = match_product_names(query, catalog)
    if names or ambiguous:
        return {"item_names": [] if ambiguous else names,
                "method": "ambiguous" if ambiguous else "exact"}
    # 明确提及未登记的型号时，不能退回上一轮设备。
    if re.search(r"(?<![A-Za-z0-9])[A-Za-z]{1,12}[- ]?\d+[A-Za-z0-9-]*", query):
        return {"item_names": [], "method": "unknown_model"}
    for message in reversed(history):
        if message.get("role") != "user":
            continue
        names, ambiguous = match_product_names(message.get("text", ""), catalog)
        if names or ambiguous:
            return {"item_names": [] if ambiguous else names,
                    "method": "ambiguous" if ambiguous else "history"}
    return {"item_names": [], "method": "missing_context"}


def topic_query(query: str, product_ids: list[str]) -> str:
    """已通过元数据限制产品后，检索关注问题内容，避免封面因型号重复而占优。"""
    text = query
    prefix = "关于" + "、".join(product_ids) + "："
    if text.startswith(prefix):
        text = text[len(prefix):]
    text = re.sub(r"^根据《[^》]+》[，,：:\s]*", "", text)
    for name in sorted(product_ids, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
        for model in re.findall(r"(?<![A-Za-z0-9])[A-Za-z]+[A-Za-z0-9-]*\d[A-Za-z0-9-]*", name):
            text = re.sub(r"(?<![A-Za-z0-9])"+re.escape(model)+r"(?![A-Za-z0-9])",
                          " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ，,：:、？?")
    return text or query
