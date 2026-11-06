# 监视批量导入任务，全部结束（成功或失败）时退出
import json
import time
from pathlib import Path

import requests

RECORD = Path(__file__).parent / "import_tasks.json"
STATUS_URL = "http://127.0.0.1:8000/status/{}"
TERMINAL = {"completed", "failed"}
TIMEOUT_MINUTES = 45

deadline = time.time() + TIMEOUT_MINUTES * 60
last_summary = None

while time.time() < deadline:
    rec = json.loads(RECORD.read_text(encoding="utf-8"))
    counts = {}
    for tid, info in rec["tasks"].items():
        try:
            s = requests.get(STATUS_URL.format(tid), timeout=15).json()
        except Exception:
            continue
        if s["status"] != info["status"]:
            info["status"] = s["status"]
            print(f"[状态变化] {info['file']} -> {s['status']}", flush=True)
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    RECORD.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = json.dumps(counts, ensure_ascii=False)
    if summary != last_summary:
        print(f"[汇总] {summary}", flush=True)
        last_summary = summary
    if set(counts) <= TERMINAL:
        print("[监视结束] 所有任务已出结果", flush=True)
        break
    time.sleep(30)
else:
    print("[监视超时] 45 分钟未全部完成", flush=True)