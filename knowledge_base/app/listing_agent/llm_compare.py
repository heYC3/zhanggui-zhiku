"""同一批冻结草稿题上，对照原句模板和真实模型。

检索只做一次，两边看同一份证据。离线回归不走这里。
费用未知时写成空值，表格里显示「未知」，不写成 0。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from app.listing_agent.catalog import get_product
from app.listing_agent.drafting import build_draft
from app.listing_agent.llm_draft import OpenAICompatibleClient, generate_llm_draft
from app.listing_agent.paths import DATA_DIR, EVAL_PATH
from app.listing_agent.retrieve import search_evidence


_CONTROL_PREFIXES = ("先确认", "资料不足", "草稿型号是")
COMPARE_DIR = DATA_DIR / "reports"


def _points(task: dict) -> list[str]:
    points = []
    for point in task.get("标准要点") or []:
        if str(point).startswith(_CONTROL_PREFIXES):
            continue
        points.append(str(point))
    if not points and task.get("expect_in_text"):
        points.append(str(task["expect_in_text"]))
    return points


def _text(draft: dict | None) -> str:
    if not draft:
        return ""
    bullets = draft.get("bullets") or []
    return (draft.get("title") or "") + "\n" + "\n".join(bullets)


def _factual(draft: dict | None, evidence_text: str, product: dict | None, forbid: list[str]) -> bool:
    if not draft or product is None:
        return False
    bullets = [str(item).strip() for item in (draft.get("bullets") or []) if str(item).strip()]
    if not bullets or any(item not in evidence_text for item in bullets):
        return False
    if draft.get("brand") != product["brand"] or draft.get("model") != product["model"]:
        return False
    joined = _text(draft)
    if any(word and word in joined for word in forbid):
        return False
    title = str(draft.get("title") or "").replace(product["model"], "")
    return all(number in evidence_text for number in re.findall(r"\d+", title))


def _coverage(draft: dict | None, task: dict) -> str:
    points = _points(task)
    if not points:
        return "—"
    text = _text(draft)
    hit = sum(1 for point in points if point in text)
    return f"{hit}/{len(points)}"


def _language(draft: dict | None, product: dict | None) -> float | None:
    """四项各 0.25：有标题和 1 到 5 条要点、标题含品牌和型号、要点不是残句、标题不超 80 字。

    没有草稿时不打 0 分，留空。0 分只表示四项都不满足。
    """
    if not draft or product is None:
        return None
    title = str(draft.get("title") or "").strip()
    bullets = [str(item).strip() for item in (draft.get("bullets") or []) if str(item).strip()]
    score = 0.0
    if title and 1 <= len(bullets) <= 5:
        score += 0.25
    if product["brand"] in title and product["model"] in title:
        score += 0.25
    if bullets and all(len(item) >= 8 for item in bullets):
        score += 0.25
    if title and len(title) <= 80:
        score += 0.25
    return score


def _money(value) -> str:
    if value is None:
        return "未知"
    if value == 0:
        return "0"
    return str(value)


def _load_draft_tasks() -> list[dict]:
    payload = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    return [task for task in payload["tasks"] if task.get("kind") == "draft"]


def compare_generators(tasks: list[dict] | None = None, *, client, strategy: str = "rrf_hyde") -> dict:
    rows = []
    logs = []
    for task in tasks if tasks is not None else _load_draft_tasks():
        found = search_evidence(task["query"], sku=task["sku"], strategy=strategy)
        evidence = found["evidence"]
        evidence_text = "\n".join(row.get("text") or "" for row in evidence)
        product = get_product(task["sku"])
        forbid = [str(word) for word in (task.get("forbid_in_text") or [])]
        offline_error = ""
        offline = None
        if evidence:
            offline = build_draft(task["query"], task["sku"], evidence, "template-draft-conservative")
        else:
            offline_error = "没有证据，不能生成草稿"
        llm = generate_llm_draft(
            task["query"],
            task["sku"],
            evidence,
            "amazon",
            client=client,
        )
        logs.append({"id": task["id"], "generator": "llm", **llm.telemetry})
        if offline is not None:
            logs.append(
                {
                    "id": task["id"],
                    "generator": "offline",
                    "mode": "offline",
                    "model": None,
                    "prompt_version": None,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cost": 0,
                    "usage_known": True,
                    "usage_source": "not_called",
                    "duration_ms": offline["duration_ms"],
                    "failure_type": None,
                    "usage_note": "没有调用模型，费用是 0。",
                }
            )
        rows.append(
            {
                "id": task["id"],
                "问题": task.get("问题") or task.get("query"),
                "offline": {
                    "factual": _factual(offline, evidence_text, product, forbid),
                    "coverage": _coverage(offline, task),
                    "language": _language(offline, product),
                    "cost": 0 if offline is not None else None,
                    "duration_ms": None if offline is None else offline["duration_ms"],
                    "failure_type": None if offline is not None else "no_evidence",
                    "message": offline_error,
                },
                "llm": {
                    "factual": _factual(llm.draft, evidence_text, product, forbid),
                    "coverage": _coverage(llm.draft, task),
                    "language": _language(llm.draft, product),
                    "cost": llm.telemetry.get("cost"),
                    "duration_ms": llm.telemetry.get("duration_ms"),
                    "failure_type": llm.telemetry.get("failure_type"),
                    "model": llm.telemetry.get("model"),
                    "prompt_version": llm.telemetry.get("prompt_version"),
                    "usage_known": llm.telemetry.get("usage_known"),
                    "usage_source": llm.telemetry.get("usage_source"),
                    "message": llm.message,
                },
            }
        )
    return {
        "corpus": "listing_data/eval/frozen_test.json 里的草稿题",
        "strategy": strategy,
        "language_rubric": "有标题且要点 1 到 5 条；标题含品牌和型号；要点至少 8 个字；标题不超过 80 字。每项 0.25。没有草稿时语言分留空。",
        "cost_note": "原句模板没有调用模型，费用是 0。模型费用只在 token 和单价都已知时计算；否则为 null，表格写未知。",
        "rows": rows,
        "logs": logs,
    }


def write_compare(report: dict, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "llm_compare.json"
    md_path = directory / "llm_compare.md"
    log_path = directory / "llm_draft_telemetry.jsonl"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 原句模板和真实模型对照",
        "",
        "同一批冻结草稿题，两边使用同一次检索得到的证据。",
        report["language_rubric"],
        report["cost_note"],
        "",
    ]
    models = sorted({row["llm"].get("model") for row in report["rows"] if row["llm"].get("model")})
    if models:
        lines.append("这次模型响应里的名字：" + "、".join(models) + "。")
        lines.append("")
    lines.extend(
        [
            "| 题 | 模板事实 | 模型事实 | 模板覆盖 | 模型覆盖 | 模板语言 | 模型语言 | 模板费用 | 模型费用 | 模板延迟ms | 模型延迟ms | 模型失败 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in report["rows"]:
        offline = row["offline"]
        llm = row["llm"]
        lines.append(
            "| {id} | {of} | {lf} | {oc} | {lc} | {ol} | {ll} | {om} | {lm} | {od} | {ld} | {fail} |".format(
                id=row["id"],
                of="是" if offline["factual"] else "否",
                lf="是" if llm["factual"] else "否",
                oc=offline["coverage"],
                lc=llm["coverage"],
                ol="—" if offline["language"] is None else offline["language"],
                ll="—" if llm["language"] is None else llm["language"],
                om=_money(offline["cost"]),
                lm=_money(llm["cost"]),
                od="—" if offline["duration_ms"] is None else offline["duration_ms"],
                ld="—" if llm["duration_ms"] is None else llm["duration_ms"],
                fail=llm["failure_type"] or "",
            )
        )
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    with log_path.open("w", encoding="utf-8") as handle:
        for record in report["logs"]:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"json": json_path, "markdown": md_path, "telemetry": log_path}


def main() -> None:
    parser = argparse.ArgumentParser(description="对照原句模板和真实模型草稿")
    parser.add_argument("--out", default=str(COMPARE_DIR))
    args = parser.parse_args()
    client = OpenAICompatibleClient.from_env()
    report = compare_generators(client=client)
    paths = write_compare(report, Path(args.out))
    print(f"对照表已写入 {paths['markdown']}")
    print(f"打点日志已写入 {paths['telemetry']}")


if __name__ == "__main__":
    main()
