"""给故障和并发测试用的短进程。

正常情况下打印一行结果，然后停在原地，等外面强制结束。
LISTING_CRASH_AT=before_checkpoint_save 时，检查点还没写上就退出。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from app.listing_agent.service import ListingService


def _wait_for_go() -> None:
    marker = os.getenv("LISTING_GO", "").strip()
    if not marker:
        return
    path = Path(marker)
    for _ in range(200):
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError("并发信号没有出现")


def main() -> None:
    runtime = Path(os.environ["LISTING_RUNTIME_DIR"])
    action = os.getenv("LISTING_FAULT_ACTION", "start")
    user_id = os.getenv("LISTING_FAULT_USER", "小明")
    service = ListingService(runtime)
    try:
        _wait_for_go()
        if action == "start":
            snapshot = service.start(
                os.getenv("LISTING_FAULT_QUERY", "掌柜示例耳机 X1 的蓝牙版本，写成要点"),
                user_id=user_id,
                role="editor",
                draft_mode="offline",
            )
        elif action == "confirm":
            snapshot = service.confirm(
                os.environ["LISTING_FAULT_TASK"],
                user_id,
                "editor",
                int(os.environ["LISTING_FAULT_VERSION"]),
            )
        elif action == "edit":
            snapshot = service.update_draft(
                os.environ["LISTING_FAULT_TASK"],
                user_id,
                "editor",
                int(os.environ["LISTING_FAULT_VERSION"]),
                os.getenv("LISTING_FAULT_TITLE", "掌柜示例X1 只改标题"),
                ["蓝牙版本是 5.3，打开盒盖就能和手机配对。"],
            )
        else:
            raise ValueError(f"不认识的动作：{action}")
        print(
            json.dumps(
                {
                    "ok": True,
                    "task_id": snapshot["task_id"],
                    "status": snapshot["status"],
                    "version": snapshot.get("draft_version"),
                    "user_id": snapshot.get("user_id"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        time.sleep(float(os.getenv("LISTING_FAULT_SLEEP", "30")))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        sys.exit(2)
    finally:
        service.close()


if __name__ == "__main__":
    main()
