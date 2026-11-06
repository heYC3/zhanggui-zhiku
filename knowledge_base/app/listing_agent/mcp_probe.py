"""用官方 MCP Python SDK 的 stdio 客户端走一遍服务。

SDK 2.3.0 的超时参数是秒，类型为 float。不要传 timedelta，那是调用脚本的类型问题。
服务端按逐行 JSON 收发，和这个客户端一致。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

from app.listing_agent.paths import DATA_DIR, PACKAGE_ROOT


RECORD_PATH = DATA_DIR / "reports" / "mcp_client_record.json"
_READ_TIMEOUT_SECONDS = 20.0


def _text_of(result) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        parts.append(getattr(block, "text", "") or "")
    return "\n".join(parts)


async def _run() -> dict:
    import mcp.client.stdio as stdio_mod

    captured: dict = {}
    original = stdio_mod._stop_server_process

    async def remember_exit(process):
        await original(process)
        captured["returncode"] = process.returncode
        captured["pid"] = process.pid

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.listing_agent.mcp_server"],
        cwd=str(PACKAGE_ROOT),
    )
    record: dict = {
        "sdk": f"mcp=={version('mcp')}",
        "transport": "stdio-ndjson",
        "timeout_seconds": _READ_TIMEOUT_SECONDS,
        "timeout_type": "float",
        "server_command": [sys.executable, "-m", "app.listing_agent.mcp_server"],
        "steps": [],
    }
    errlog = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    stdio_mod._stop_server_process = remember_exit
    try:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=_READ_TIMEOUT_SECONDS) as session:
                initialized = await session.initialize()
                record["steps"].append(
                    {
                        "step": "initialize",
                        "server_name": initialized.server_info.name,
                        "server_version": initialized.server_info.version,
                        "protocol_version": initialized.protocol_version,
                    }
                )
                listed = await session.list_tools()
                names = [tool.name for tool in listed.tools]
                record["steps"].append({"step": "list_tools", "tools": names})
                called = await session.call_tool(
                    "lookup_product_evidence",
                    {
                        "query": "掌柜示例耳机 X1 的蓝牙版本",
                        "actor_id": "小明",
                        "role": "editor",
                        "sku": "zg-ear-x1",
                        "strategy": "sparse",
                    },
                    read_timeout_seconds=_READ_TIMEOUT_SECONDS,
                )
                record["steps"].append(
                    {
                        "step": "call",
                        "tool": "lookup_product_evidence",
                        "is_error": bool(called.is_error),
                        "text": _text_of(called)[:800],
                    }
                )
                try:
                    invalid = await session.call_tool(
                        "lookup_product_evidence",
                        {"actor_id": "小明", "role": "editor"},
                        read_timeout_seconds=_READ_TIMEOUT_SECONDS,
                    )
                    record["steps"].append(
                        {
                            "step": "bad_arguments",
                            "tool": "lookup_product_evidence",
                            "is_error": bool(invalid.is_error),
                            "text": _text_of(invalid)[:800],
                        }
                    )
                except MCPError as exc:
                    record["steps"].append(
                        {
                            "step": "bad_arguments",
                            "tool": "lookup_product_evidence",
                            "is_error": True,
                            "text": str(exc)[:800],
                        }
                    )
    finally:
        stdio_mod._stop_server_process = original
        errlog.seek(0)
        stderr_text = errlog.read().strip()
        errlog.close()
    record["steps"].append(
        {
            "step": "process_exit",
            "pid": captured.get("pid"),
            "returncode": captured.get("returncode"),
        }
    )
    if stderr_text:
        record["stderr_tail"] = stderr_text[-500:]
    return record


def run_official_client(record_path: Path | None = None) -> dict:
    record = asyncio.run(_run())
    path = Path(record_path) if record_path else RECORD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    record["record_path"] = str(path)
    return record


def main() -> None:
    record = run_official_client()
    print(f"调用记录已写入 {record['record_path']}")


if __name__ == "__main__":
    main()
