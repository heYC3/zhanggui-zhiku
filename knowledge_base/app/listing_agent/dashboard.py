"""只读取在线运行目录的日志；每个任务只计一次，缺用量保持未知。"""

import json
import math
from pathlib import Path


def _latest(path: Path) -> dict:
    records = {}
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 并发追加时可能正在写最后一行。
                if isinstance(row, dict) and row.get("trace_id"):
                    records[row["trace_id"]] = row
    return records


def _sum_known(values):
    values = list(values)
    if any(not isinstance(value, (int, float)) or isinstance(value, bool)
           or not math.isfinite(value) or value < 0 for value in values):
        return None
    return round(sum(values), 8)


def summarize(runtime: Path, tasks: list[dict]) -> dict:
    telemetry = _latest(runtime / "draft_telemetry.jsonl")
    traces = _latest(runtime / "traces.jsonl")
    result = []
    for task in tasks:
        task_id = task["task_id"]
        record = telemetry.get(task_id)
        trace = traces.get(task_id, {})
        if record is not None:
            attempts = record.get("attempts") or []
            if attempts:
                usage = {key: _sum_known(attempt.get(key) for attempt in attempts)
                         for key in ("prompt_tokens", "completion_tokens", "cost")}
                count = len(attempts)
            elif record.get("mode") == "offline":
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0}
                count = 0
            else:
                usage = {key: record.get(key) for key in ("prompt_tokens", "completion_tokens", "cost")}
                count = 1
        else:
            usage = trace.get("usage") or {}
            count = None
        result.append({**task, **{key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "cost")},
                       "provider_attempts": count})
    return {"task_count": len(result), "tasks": result,
            "totals": {key: _sum_known(row[key] for row in result)
                       for key in ("prompt_tokens", "completion_tokens", "cost")},
            "source": "online_runtime", "cost_currency": "USD"}
