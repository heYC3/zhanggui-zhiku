"""在同一个持久目录里分两次启动。

第一次留下等待确认的任务。第二次重新打开，确认只能写入一次。
容器卷和本机目录用的是同一段代码。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.listing_agent.paths import runtime_dir
from app.listing_agent.service import ListingService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("one", "two"), required=True)
    args = parser.parse_args()
    runtime = runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    marker = runtime / "volume_probe.json"
    service = ListingService(runtime)
    try:
        if args.phase == "one":
            snapshot = service.start(
                "掌柜示例耳机 X1 的蓝牙版本，写成要点",
                user_id="卷检查",
                role="editor",
                draft_mode="offline",
            )
            if snapshot["status"] != "awaiting_confirmation":
                raise SystemExit(f"第一次应停在确认，实际是 {snapshot['status']}")
            marker.write_text(
                json.dumps(
                    {"task_id": snapshot["task_id"], "version": snapshot["draft_version"]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            print(f"phase one task {snapshot['task_id']} awaiting")
            return
        saved = json.loads(marker.read_text(encoding="utf-8"))
        current = service.snapshot(saved["task_id"])
        if current["status"] != "awaiting_confirmation":
            raise SystemExit(f"重启后应仍在确认，实际是 {current['status']}")
        written = service.confirm(saved["task_id"], "卷检查", "editor", int(saved["version"]))
        if written["status"] != "written":
            raise SystemExit(f"确认后应写入，实际是 {written['status']}")
        if service.ledger.listing_count(saved["task_id"]) != 1:
            raise SystemExit("重启后的确认写了不止一次")
        print("phase two written once")
    finally:
        service.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise
