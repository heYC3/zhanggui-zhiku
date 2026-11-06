"""把规则校验和资料检索做成 MCP 工具。

网页确认写入仍然走 Listing 服务，不从这里直接写台账。
操作说明见同目录 SKILL.md。日志只打到 stderr，避免弄乱 MCP 的标准输出。
"""

from __future__ import annotations

import json
import sys

from app.listing_agent.retrieve import search_evidence
from app.listing_agent.rules import Actor, validate_listing_draft


_TOOLS = [
    {
        "name": "lookup_product_evidence",
        "description": "从自编商品资料里查证据。需要操作者身份。不会写入 Listing。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "sku": {"type": "string"},
                "strategy": {"type": "string", "enum": ["sparse", "dense", "rrf", "rrf_hyde"]},
                "actor_id": {"type": "string"},
                "role": {"type": "string", "enum": ["editor", "viewer"]},
            },
            "required": ["query", "actor_id", "role"],
        },
    },
    {
        "name": "validate_listing_draft",
        "description": "用和网页确认相同的规则检查一份 Listing 草稿。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft": {"type": "object"},
                "platform": {"type": "string"},
                "actor_id": {"type": "string"},
                "role": {"type": "string", "enum": ["editor", "viewer"]},
            },
            "required": ["draft", "platform", "actor_id", "role"],
        },
    },
]


def _tool_result(payload: dict, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": is_error,
    }


def call_tool(name: str, arguments: dict) -> dict:
    try:
        actor = Actor(arguments.get("actor_id", ""), arguments.get("role", ""))
        if name == "lookup_product_evidence":
            actor.require(("editor", "viewer"))
            result = search_evidence(
                arguments.get("query") or "",
                sku=arguments.get("sku") or None,
                strategy=arguments.get("strategy") or "rrf_hyde",
            )
            return _tool_result(
                {
                    "evidence": result["evidence"],
                    "meta": result["meta"],
                }
            )
        if name == "validate_listing_draft":
            validation = validate_listing_draft(
                arguments.get("draft") or {},
                arguments.get("platform") or "amazon",
                actor,
            )
            return _tool_result(validation, is_error=not validation["ok"])
        return _tool_result({"message": f"没有这个工具：{name}"}, is_error=True)
    except PermissionError as exc:
        return _tool_result({"message": str(exc)}, is_error=True)
    except Exception as exc:  # 把工具错误交回调用方，不把协议连接弄断
        return _tool_result({"message": str(exc)}, is_error=True)


def handle(message: dict) -> dict | None:
    method = message.get("method")
    if message.get("id") is None:
        return None
    if method == "initialize":
        return _ok(
            message["id"],
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "zhanggui-listing", "version": "0.1.0"},
            },
        )
    if method == "tools/list":
        return _ok(message["id"], {"tools": _TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        return _ok(message["id"], call_tool(params.get("name") or "", params.get("arguments") or {}))
    return _error(message["id"], f"不支持的方法：{method}")


def _ok(message_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": -32601, "message": text}}


def _read_message() -> dict | None:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode("utf-8").split(":", 1)
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def _write_message(payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def main() -> None:
    while True:
        message = _read_message()
        if message is None:
            return
        response = handle(message)
        if response is not None:
            _write_message(response)


if __name__ == "__main__":
    main()
