# 批量上传手册到导入服务（自动跳过已入库的）
# 用法:
#   python batch_import.py wave 0 8   # 上传"剩余手册"里第 0~7 份
#   python batch_import.py status     # 查看已记录任务的进度
#   python batch_import.py remaining  # 看还剩几份没入库
import json
import os
import sys
from pathlib import Path

import requests

doc_dir = os.getenv("IMPORT_DOC_DIR", "")
if not doc_dir:
    sys.exit("请设置 IMPORT_DOC_DIR，指向要导入的手册目录")
DOC_DIR = Path(doc_dir)
UPLOAD_URL = "http://127.0.0.1:8000/upload"
STATUS_URL = "http://127.0.0.1:8000/status/{}"
MILVUS_URI = os.getenv("MILVUS_URL", "http://127.0.0.1:19530")
RECORD = Path(__file__).parent / "import_tasks.json"


def load_record() -> dict:
    if RECORD.exists():
        return json.loads(RECORD.read_text(encoding="utf-8"))
    return {"tasks": {}}


def save_record(rec: dict) -> None:
    RECORD.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def completed_titles() -> set:
    """从 Milvus 的 kb_item_names 读已入库手册的文件名（不含扩展名）"""
    try:
        from pymilvus import MilvusClient

        c = MilvusClient(uri=MILVUS_URI)
        c.load_collection("kb_item_names")
        rows = c.query("kb_item_names", filter="pk >= 0", output_fields=["file_title"], limit=1000)
        return {r["file_title"] for r in rows}
    except Exception as e:
        print(f"[警告] 读 Milvus 失败（{e}），不跳过任何文件")
        return set()


def remaining_pdfs() -> list:
    """全部 PDF - 已入库的 - 在途的（processing/pending），按体积从小到大"""
    done = completed_titles()
    rec = load_record()
    in_flight = {
        Path(info["file"]).stem
        for info in rec["tasks"].values()
        if info.get("status") in ("processing", "pending", "")
    }
    pdfs = sorted(DOC_DIR.glob("*.pdf"), key=lambda p: p.stat().st_size)
    remaining = [p for p in pdfs if p.stem not in done and p.stem not in in_flight]
    print(f"目录共 {len(pdfs)} 份，已入库 {len(pdfs) - len(remaining) - len(in_flight)} 份，"
          f"在途 {len(in_flight)} 份，本次可传 {len(remaining)} 份")
    return remaining


def wave(start: int, count: int) -> None:
    rec = load_record()
    files = remaining_pdfs()[start : start + count]
    if not files:
        print("这一批没有文件（可能全部已入库）")
        return
    payload = [("files", (f.name, open(f, "rb"), "application/pdf")) for f in files]
    r = requests.post(UPLOAD_URL, files=payload, timeout=300)
    r.raise_for_status()
    task_ids = r.json()["task_ids"]
    for f, tid in zip(files, task_ids):
        rec["tasks"][tid] = {"file": f.name, "status": "pending"}
        print(f"已上传 {f.name} -> {tid}")
    save_record(rec)


def status() -> None:
    rec = load_record()
    if not rec["tasks"]:
        print("还没有任务")
        return
    changed = False
    for tid, info in rec["tasks"].items():
        try:
            s = requests.get(STATUS_URL.format(tid), timeout=15).json()
        except Exception as e:
            print(f"[网络错误] {info['file']}: {e}")
            continue
        if s["status"] != info["status"]:
            info["status"] = s["status"]
            changed = True
        mark = {"completed": "✅", "failed": "❌", "processing": "⏳", "pending": "🕐"}.get(
            s["status"], "❔"
        )
        done = len(s.get("done_list", []))
        print(f"{mark} [{s['status']:10}] 已完成{done}节点  {info['file']}")
    if changed:
        save_record(rec)
    counts = {}
    for info in rec["tasks"].values():
        counts[info["status"]] = counts.get(info["status"], 0) + 1
    print("汇总:", counts)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "wave":
        wave(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "status":
        status()
    elif cmd == "remaining":
        remaining_pdfs()
    else:
        print("用法: wave <起始序号> <份数> | status | remaining")