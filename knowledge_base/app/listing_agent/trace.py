"""每次任务写一条本地 trace。配了 Langfuse 密钥就再送一份，没配也不影响使用。"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path


logger = logging.getLogger("listing_agent.trace")


def write_trace(directory: Path, record: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "traces.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    _try_langfuse(record)
    return path


def _try_langfuse(record: dict) -> None:
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        return
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    body = {
        "batch": [
            {
                "id": record["trace_id"],
                "type": "trace-create",
                "timestamp": record["ended_at"],
                "body": {
                    "id": record["trace_id"],
                    "name": "listing-agent",
                    "userId": record.get("user_id", ""),
                    "sessionId": record.get("session_id", ""),
                    "input": {"query": record.get("query", "")},
                    "output": {"status": record.get("status", "")},
                    "metadata": {
                        "strategy": record.get("strategy", ""),
                        "embedding": record.get("embedding", ""),
                        "reranker": record.get("reranker", ""),
                        "draft_policy": record.get("draft_policy", ""),
                    },
                },
            }
        ]
    }
    request = urllib.request.Request(
        f"{host}/api/public/ingestion",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Langfuse 发送失败，本地 trace 已保留：%s", exc)
