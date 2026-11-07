"""本机 Shopify 验收入口。默认只核对；显式 --create-demo --confirm 才建草稿。"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from app.listing_agent.paths import runtime_dir
from app.listing_agent.service import ListingService


def main():
    from app.listing_agent.shopify_adapter import load_env_file
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__)
    default_runtime = Path(os.environ["LISTING_RUNTIME_DIR"]) if os.getenv("LISTING_RUNTIME_DIR") else runtime_dir() / "shopify-step2"
    parser.add_argument("--runtime", type=Path, default=default_runtime)
    parser.add_argument("--task-id", help="已存在的任务；省略时核对最新写入意图")
    parser.add_argument("--create-demo", action="store_true", help="生成固定幂等键的自编示例任务")
    parser.add_argument("--confirm", action="store_true", help="确认示例草稿写入测试店")
    parser.add_argument("--output", type=Path, help="脱敏报告路径；默认保存到运行目录")
    args = parser.parse_args()
    if args.confirm and not args.create_demo:
        parser.error("--confirm 必须与 --create-demo 一起使用；已有任务请在工作台确认")
    service = ListingService(args.runtime, shop_backend="shopify")
    try:
        if args.create_demo:
            task = service.start(
                "掌柜示例耳机 X1 的蓝牙版本，写成要点", user_id="shopify-acceptance",
                role="editor", org_id="local-acceptance", draft_mode="offline",
                idempotency_key="shopify-step2-demo-v1",
            )
            if args.confirm:
                task = service.confirm(task["task_id"], "shopify-acceptance", "editor", task["draft_version"])
        elif args.task_id:
            task = service.snapshot(args.task_id)
        else:
            row = service.shop_writer.db.execute("SELECT task_id FROM write_intents ORDER BY created_at DESC LIMIT 1").fetchone()
            if not row:
                raise SystemExit("没有待核对意图。请先在工作台确认，或显式使用 --create-demo --confirm。")
            task = service.snapshot(row["task_id"])
        intent_row = service.shop_writer.get(task["task_id"], task["draft_version"])
        if not intent_row:
            print(json.dumps({"task_id": task["task_id"], "status": task["status"], "note": "尚未确认，没有外部写入意图"}, ensure_ascii=False))
            return
        # 这里显式发起远端查询，不把本地缓存当作本次读回证据。
        result = service.shop_writer.adapter.reconcile(service.shop_writer.intent(intent_row))
        receipt = task.get("receipt") or {}
        report = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "store_domain": intent_row["store_domain"], "api_version": "2026-10",
            "task_id": task["task_id"], "draft_version": task["draft_version"],
            "operation_id": intent_row["operation_id"], "payload_sha256": intent_row["payload_sha256"],
            "local_status": task["status"], "shop_product_id": receipt.get("shop_product_id"),
            "shop_status": (result.product or {}).get("status"),
            "remote_reconcile": result.status, "reason": result.reason,
            "product": result.product, "url": receipt.get("url"),
            "mutation_confirmed": (receipt.get("raw") or {}).get("mutation_confirmed"),
            "readback_confirmed": (receipt.get("raw") or {}).get("readback_confirmed"),
            "scope": "指定开发店、自编商品、仅 DRAFT；不证明真实模型或生产部署",
        }
        output = args.output or args.runtime / "shopify_receipt.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"report": str(output), "task_id": task["task_id"], "shop_product_id": report["shop_product_id"],
                          "shop_status": report["shop_status"], "remote_reconcile": result.status}, ensure_ascii=False))
        if result.status != "found":
            raise SystemExit(2)
    finally:
        service.close()


if __name__ == "__main__":
    main()
