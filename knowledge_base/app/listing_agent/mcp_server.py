"""把规则校验和资料检索做成 MCP 工具。

传输使用官方 Python SDK 的 stdio：一行一条 JSON，不使用 Content-Length 帧。
网页确认写入仍然走 Listing 服务，这里不提供写台账的工具。
日志只打到 stderr，避免弄乱标准输出上的协议。
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.listing_agent.retrieve import search_evidence
from app.listing_agent.rules import Actor, validate_listing_draft


logger = logging.getLogger("listing_agent.mcp")

_STRATEGIES = ("sparse", "dense", "rrf", "rrf_hyde")


def _tool_result(payload: dict, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": is_error,
    }


def call_tool(name: str, arguments: dict) -> dict:
    """供测试和官方 SDK 工具共用。不写入台账。"""
    try:
        actor = Actor(arguments.get("actor_id", ""), arguments.get("role", ""))
        if name == "lookup_product_evidence":
            actor.require(("editor", "viewer"))
            strategy = arguments.get("strategy") or "rrf_hyde"
            if strategy not in _STRATEGIES:
                return _tool_result({"message": "检索策略不在允许的范围内"}, is_error=True)
            result = search_evidence(
                arguments.get("query") or "",
                sku=arguments.get("sku") or None,
                strategy=strategy,
            )
            return _tool_result({"evidence": result["evidence"], "meta": result["meta"]})
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
        logger.exception("MCP 工具执行失败")
        return _tool_result({"message": str(exc)}, is_error=True)


def _raise_if_error(result: dict) -> str:
    text = result["content"][0]["text"]
    if result["isError"]:
        try:
            message = json.loads(text).get("message") or text
        except json.JSONDecodeError:
            message = text
        raise ToolError(message)
    return text


def build_server() -> MCPServer:
    """两个只读工具。名字和原来的自测工具一致，不增加写操作。"""
    server = MCPServer(
        "zhanggui-listing",
        version="0.1.0",
        instructions="检索自编商品证据，或按平台规则检查草稿。这两个工具都不写入台账。",
    )

    @server.tool(
        name="lookup_product_evidence",
        description="从自编商品资料里查证据。需要操作者身份。不会写入 Listing。",
    )
    def lookup_product_evidence(
        query: str,
        actor_id: str,
        role: Literal["editor", "viewer"],
        sku: str = "",
        strategy: Literal["sparse", "dense", "rrf", "rrf_hyde"] = "rrf_hyde",
    ) -> str:
        return _raise_if_error(
            call_tool(
                "lookup_product_evidence",
                {
                    "query": query,
                    "sku": sku,
                    "strategy": strategy,
                    "actor_id": actor_id,
                    "role": role,
                },
            )
        )

    @server.tool(
        name="validate_listing_draft",
        description="用和网页确认相同的规则检查一份 Listing 草稿。不会写入。",
    )
    def validate_listing_draft_tool(
        draft: dict,
        platform: str,
        actor_id: str,
        role: Literal["editor", "viewer"],
    ) -> str:
        return _raise_if_error(
            call_tool(
                "validate_listing_draft",
                {
                    "draft": draft,
                    "platform": platform,
                    "actor_id": actor_id,
                    "role": role,
                },
            )
        )

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
