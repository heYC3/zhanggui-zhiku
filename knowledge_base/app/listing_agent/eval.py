"""在固定任务集上对照检索策略和两种草稿写法。

不调用付费模型。数字只描述这份自编资料，不能写成线上提升。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from app.listing_agent.catalog import resolve_product
from app.listing_agent.drafting import build_draft
from app.listing_agent.paths import BASELINE_PATH, EVAL_PATH, MODEL_CARD_PATH, REPORT_PATH
from app.listing_agent.retrieve import search_evidence
from app.listing_agent.service import ListingService


_STRATEGIES = ("sparse", "dense", "rrf", "rrf_hyde")


def _load_tasks() -> list[dict]:
    payload = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    return payload["tasks"]


def _hit(query: str, sku: str, strategy: str, expect_chunk: str, rerank_mode: str = "on") -> dict:
    result = search_evidence(query, sku=sku, strategy=strategy, rerank_mode=rerank_mode)
    found = [row["id"] for row in result["evidence"]]
    return {
        "hit": expect_chunk in found,
        "top": found,
        "duration_ms": result["meta"]["duration_ms"],
        "meta": result["meta"],
    }


def _workflow_checks(tasks: list[dict]) -> list[dict]:
    checks = []
    with tempfile.TemporaryDirectory() as directory:
        service = ListingService(Path(directory))
        try:
            for task in tasks:
                if task["kind"] != "workflow":
                    continue
                if task.get("history"):
                    session_id = f"eval-{task['id']}"
                    for text in task["history"]:
                        service.ledger.append_message(session_id, text, "")
                    result = service.start(
                        task["query"],
                        user_id="eval-editor",
                        role="editor",
                        session_id=session_id,
                    )
                    passed = result.get("sku") == task["expect_sku"] and bool(result.get("draft"))
                else:
                    result = service.start(task["query"], user_id="eval-editor", role="editor")
                    passed = result.get("status") == task["expect_status"] and not result.get("draft")
                checks.append(
                    {
                        "id": task["id"],
                        "题型": task.get("题型", ""),
                        "pass": passed,
                        "sku": result.get("sku"),
                        "status": result.get("status"),
                    }
                )
        finally:
            service.close()
    return checks


# 完成率的 k。标准要点里属于资料原句的，至少要命中这么多条。
POINT_MIN = 1
_CONTROL_PREFIXES = ("先确认", "资料不足", "草稿型号是")
_PLATFORM_FORBIDDEN = ("最", "第一", "100%", "全网")


def _completion_tasks(tasks: list[dict]) -> list[dict]:
    return [task for task in tasks if task["kind"] in {"draft", "workflow"}]


def _content_points(task: dict) -> list[str]:
    points = []
    for point in task.get("标准要点") or []:
        if str(point).startswith(_CONTROL_PREFIXES):
            continue
        points.append(str(point))
    return points


def _chunk_id(task: dict) -> str:
    if task.get("expect_chunk"):
        return task["expect_chunk"]
    where = str(task.get("证据位置") or "")
    if "#" in where:
        return where.rsplit("#", 1)[-1]
    return ""


def _forbidden_words(task: dict) -> list[str]:
    words = [str(word) for word in (task.get("forbid_in_text") or [])]
    for word in _PLATFORM_FORBIDDEN:
        if word not in words:
            words.append(word)
    return words


def _draft_text(snapshot: dict) -> str:
    draft = snapshot.get("draft") or {}
    bullets = draft.get("bullets") or []
    return (draft.get("title") or "") + "\n" + "\n".join(bullets)


def _judge_task(task: dict, snapshot: dict, *, used_gate: bool) -> dict:
    """一条题一个布尔值。五项同时为真才算成功。流程题才检查确认路径。"""
    draft = snapshot.get("draft") or {}
    text = _draft_text(snapshot)
    evidence_ids = [row.get("id") for row in (snapshot.get("evidence") or [])]
    sku = snapshot.get("sku") or ""
    status = snapshot.get("status") or ""
    validation = snapshot.get("validation") or {}
    checks: dict[str, bool] = {}

    if task["kind"] == "draft" or task.get("expect_sku"):
        expected = task.get("expect_sku") or task.get("sku")
        checks["型号对"] = sku == expected and bool(draft)
    else:
        checks["型号对"] = status == task.get("expect_status") and not sku and not draft

    chunk = _chunk_id(task)
    if chunk:
        checks["证据命中"] = chunk in evidence_ids
    else:
        checks["证据命中"] = not draft

    points = _content_points(task)
    if points:
        checks["要点"] = sum(1 for point in points if point in text) >= POINT_MIN
    else:
        checks["要点"] = True

    if draft:
        checks["禁语"] = not any(word and word in text for word in _forbidden_words(task))
    else:
        checks["禁语"] = True

    if task["kind"] == "workflow":
        if task.get("expect_status"):
            checks["路径"] = status == task["expect_status"] and not draft
        else:
            checks["路径"] = (
                used_gate
                and status == "written"
                and bool(validation.get("ok"))
                and not validation.get("skipped")
            )

    return {
        "id": task["id"],
        "kind": task["kind"],
        "题型": task.get("题型", ""),
        "success": all(checks.values()),
        "checks": checks,
        "status": status,
        "sku": sku,
    }


def _run_completion_config(tasks: list[dict], *, name: str, label: str, strategy: str, rerank_mode: str, skip_confirmation: bool, skip_rules: bool) -> dict:
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        service = ListingService(Path(directory))
        try:
            for task in tasks:
                session_id = f"cmp-{name}-{task['id']}"
                for text in task.get("history") or []:
                    service.ledger.append_message(session_id, text, "")
                started = service.start(
                    task["query"],
                    user_id="eval-editor",
                    role="editor",
                    session_id=session_id,
                    strategy=strategy,
                    rerank_mode=rerank_mode,
                    skip_confirmation=skip_confirmation,
                    skip_rules=skip_rules,
                )
                used_gate = started.get("status") == "awaiting_confirmation"
                final = started
                if used_gate:
                    validation = started.get("validation") or {}
                    if validation.get("ok") and not validation.get("skipped"):
                        final = service.confirm(
                            started["task_id"],
                            "eval-editor",
                            "editor",
                            int(started.get("draft_version") or 0),
                        )
                rows.append(_judge_task(task, final, used_gate=used_gate))
        finally:
            service.close()
    success = sum(1 for row in rows if row["success"])
    total = len(rows)
    return {
        "name": name,
        "label": label,
        "strategy": strategy,
        "rerank_mode": rerank_mode,
        "skip_confirmation": skip_confirmation,
        "skip_rules": skip_rules,
        "success": success,
        "total": total,
        "rate": f"{success}/{total}",
        "percent": round(success / total * 100, 1) if total else 0,
        "ids": [row["id"] for row in rows],
        "tasks": rows,
    }


def _completion(tasks: list[dict]) -> dict:
    population = _completion_tasks(tasks)
    before = _run_completion_config(
        population,
        name="before",
        label="关键词检索，无确认门，无规则校验",
        strategy="sparse",
        rerank_mode="off",
        skip_confirmation=True,
        skip_rules=True,
    )
    after = _run_completion_config(
        population,
        name="after",
        label="当前默认链路：RRF+HyDE、确认、规则、重排序可降级",
        strategy="rrf_hyde",
        rerank_mode="on",
        skip_confirmation=False,
        skip_rules=False,
    )
    return {
        "population": "frozen draft+workflow",
        "point_min": POINT_MIN,
        "total": len(population),
        "criteria": [
            "型号对",
            "证据命中标准位置",
            "草稿含标准要点至少 1 条",
            "禁语 0",
            "流程题的确认路径正确",
        ],
        "before": before,
        "after": after,
    }


def _rate(rows: list[dict], strategy: str, field: str) -> str:
    if not rows:
        return "—"
    hit = sum(1 for row in rows if row["strategies"][strategy][field])
    return f"{hit}/{len(rows)}"


def run_eval() -> dict:
    tasks = _load_tasks()
    model_card = json.loads(MODEL_CARD_PATH.read_text(encoding="utf-8"))
    retrieval_rows = []
    for task in tasks:
        if task["kind"] != "retrieval":
            continue
        by_strategy = {}
        for strategy in _STRATEGIES:
            measured = _hit(task["query"], task["sku"], strategy, task["expect_chunk"])
            by_strategy[strategy] = {
                "hit_at_1": bool(measured["top"] and measured["top"][0] == task["expect_chunk"]),
                "hit_at_3": measured["hit"],
                "duration_ms": measured["duration_ms"],
                "top": measured["top"],
            }
        degraded = _hit(task["query"], task["sku"], "rrf_hyde", task["expect_chunk"], rerank_mode="down")
        retrieval_rows.append(
            {
                "id": task["id"],
                "题型": task.get("题型", ""),
                "purpose": task["purpose"],
                "expect_chunk": task["expect_chunk"],
                "strategies": by_strategy,
                "rerank_down": {
                    "degraded": degraded["meta"]["rerank_degraded"],
                    "hit_at_3": degraded["hit"],
                    "reranker": degraded["meta"]["reranker"],
                },
            }
        )

    draft_rows = []
    for task in tasks:
        if task["kind"] != "draft":
            continue
        for strategy in ("sparse", "rrf_hyde"):
            found = search_evidence(task["query"], sku=task["sku"], strategy=strategy)
            for policy in ("template-draft-conservative", "template-draft-fuller"):
                if not found["evidence"]:
                    draft_rows.append(
                        {
                            "id": task["id"],
                            "strategy": strategy,
                            "policy": policy,
                            "has_expected": False,
                            "forbidden_hit": False,
                            "bullet_count": 0,
                            "duration_ms": found["meta"]["duration_ms"],
                        }
                    )
                    continue
                draft = build_draft(task["query"], task["sku"], found["evidence"], policy)
                text = draft["title"] + "\n" + "\n".join(draft["bullets"])
                draft_rows.append(
                    {
                        "id": task["id"],
                        "strategy": strategy,
                        "policy": policy,
                        "has_expected": task["expect_in_text"] in text,
                        "forbidden_hit": any(word in text for word in task["forbid_in_text"]),
                        "bullet_count": len(draft["bullets"]),
                        "duration_ms": round(found["meta"]["duration_ms"] + draft["duration_ms"], 3),
                        "cost": 0,
                    }
                )

    workflow = _workflow_checks(tasks)
    paraphrase = next(row for row in retrieval_rows if row["id"] == "paraphrase-battery")
    sparse_hit = paraphrase["strategies"]["sparse"]["hit_at_3"]
    hyde_hit = paraphrase["strategies"]["rrf_hyde"]["hit_at_3"]
    conclusion = (
        "在这条把「能听多久」换成口语的问法上，关键词召回没有找回「连续播放 30 小时」，"
        "RRF 加上模板 HyDE 之后找回了。"
        if (not sparse_hit and hyde_hit)
        else "这次对照里，HyDE 没有比关键词多找回目标证据，不能写成有收益。"
    )
    conclusion += "重排序被关掉时，结果退回 RRF，并在记录里标明降级。两种草稿器费用都是 0，因为没有调用付费模型。"
    baseline = _baseline_table(retrieval_rows, draft_rows, workflow)
    completion = _completion(tasks)
    report = {
        "corpus": "listing_data/products 自编 3 个商品，synthetic，不是在售商品",
        "split": "frozen_test",
        "task_count": len(tasks),
        "model_card": {
            "draft_generator": model_card["draft_generator"],
            "embedding": model_card["embedding"],
            "reranker": model_card["reranker"],
            "chat_model_live": model_card["chat_model_live"],
        },
        "retrieval": retrieval_rows,
        "drafts": draft_rows,
        "workflow": workflow,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0},
        "baseline": baseline,
        "completion": {
            "population": completion["population"],
            "point_min": completion["point_min"],
            "total": completion["total"],
            "criteria": completion["criteria"],
            "before": completion["before"]["rate"],
            "before_percent": completion["before"]["percent"],
            "before_label": completion["before"]["label"],
            "after": completion["after"]["rate"],
            "after_percent": completion["after"]["percent"],
            "after_label": completion["after"]["label"],
            "tasks": {
                "before": completion["before"]["tasks"],
                "after": completion["after"]["tasks"],
            },
        },
        "conclusion": conclusion,
    }
    return report


def _baseline_table(retrieval_rows: list[dict], draft_rows: list[dict], workflow: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for row in retrieval_rows:
        groups.setdefault(row.get("题型") or "未标", []).append(row)
    retrieval_summary = []
    for name in ("关键词检索", "改写检索", "型号区分"):
        rows = groups.get(name, [])
        if not rows:
            continue
        retrieval_summary.append(
            {
                "题型": name,
                "条数": len(rows),
                "关键词 hit@1": _rate(rows, "sparse", "hit_at_1"),
                "关键词 hit@3": _rate(rows, "sparse", "hit_at_3"),
                "稠密 hit@1": _rate(rows, "dense", "hit_at_1"),
                "稠密 hit@3": _rate(rows, "dense", "hit_at_3"),
                "RRF hit@1": _rate(rows, "rrf", "hit_at_1"),
                "RRF hit@3": _rate(rows, "rrf", "hit_at_3"),
                "RRF+HyDE hit@1": _rate(rows, "rrf_hyde", "hit_at_1"),
                "RRF+HyDE hit@3": _rate(rows, "rrf_hyde", "hit_at_3"),
            }
        )
    if retrieval_rows:
        retrieval_summary.append(
            {
                "题型": "检索合计",
                "条数": len(retrieval_rows),
                "关键词 hit@1": _rate(retrieval_rows, "sparse", "hit_at_1"),
                "关键词 hit@3": _rate(retrieval_rows, "sparse", "hit_at_3"),
                "稠密 hit@1": _rate(retrieval_rows, "dense", "hit_at_1"),
                "稠密 hit@3": _rate(retrieval_rows, "dense", "hit_at_3"),
                "RRF hit@1": _rate(retrieval_rows, "rrf", "hit_at_1"),
                "RRF hit@3": _rate(retrieval_rows, "rrf", "hit_at_3"),
                "RRF+HyDE hit@1": _rate(retrieval_rows, "rrf_hyde", "hit_at_1"),
                "RRF+HyDE hit@3": _rate(retrieval_rows, "rrf_hyde", "hit_at_3"),
            }
        )
    conservative = [row for row in draft_rows if row["policy"] == "template-draft-conservative"]
    workflow_pass = sum(1 for item in workflow if item["pass"])
    return {
        "retrieval": retrieval_summary,
        "draft_conservative": {
            "条数": len(conservative),
            "写出标准要点": f"{sum(1 for row in conservative if row['has_expected'])}/{len(conservative)}" if conservative else "—",
            "写出禁用说法": f"{sum(1 for row in conservative if row['forbidden_hit'])}/{len(conservative)}" if conservative else "—",
            "费用": 0,
        },
        "workflow": {
            "条数": len(workflow),
            "通过": f"{workflow_pass}/{len(workflow)}" if workflow else "—",
        },
    }


def write_baseline(report: dict, path: Path) -> None:
    lines = [
        "# Listing 冻结测集基线",
        "",
        "语料是 3 个自编商品，不是在售商品。检索是离线字块，没有加载 BGE-M3，也没有调用付费模型。",
        "开发集不在这张表里。下面先写任务完成率。再往下的 hit@k 只说明检索排位，不是完成率。",
        "",
        "## 任务完成率",
        "",
        "一条题一次任务，成功或失败。只统计冻结集里的草稿题和流程题。",
        "成功要同时满足：型号对；证据命中标准位置；资料要点至少写进草稿 1 条；禁语 0 条。",
        "流程题还要路径正确：该停的题停住且不写草稿；该写的题先等待确认，规则通过，编辑确认后才写入。",
        "Before 关掉后来加的能力：只用关键词，不重排序，没有确认门，也不做规则校验。After 是当前默认链路。",
        "问答完成率不在这张表里。冻结集没有「答案是否覆盖标准要点」的问答题。",
        "",
        "| 配置 | 成功/总数 | 完成率 |",
        "| --- | --- | --- |",
        "| Before：{before_label} | {before} | {before_percent}% |".format(**report["completion"]),
        "| After：{after_label} | {after} | {after_percent}% |".format(**report["completion"]),
        "",
        "## 检索排位（不是完成率）",
        "",
        "hit@1 是目标证据排第一，hit@3 是出现在前 3 条。",
        "",
        "| 题型 | 条数 | 关键词 hit@1 | 关键词 hit@3 | 稠密 hit@1 | 稠密 hit@3 | RRF hit@1 | RRF hit@3 | RRF+HyDE hit@1 | RRF+HyDE hit@3 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["baseline"]["retrieval"]:
        lines.append(
            "| {题型} | {条数} | {关键词 hit@1} | {关键词 hit@3} | {稠密 hit@1} | {稠密 hit@3} | {RRF hit@1} | {RRF hit@3} | {RRF+HyDE hit@1} | {RRF+HyDE hit@3} |".format(**row)
        )
    draft = report["baseline"]["draft_conservative"]
    flow = report["baseline"]["workflow"]
    lines.extend(
        [
            "",
            f"保守草稿 {draft['条数']} 条：写出标准要点 {draft['写出标准要点']}，写出禁用说法 {draft['写出禁用说法']}，费用 {draft['费用']}。",
            f"流程题 {flow['条数']} 条：通过 {flow['通过']}。",
            "",
            report["conclusion"],
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def assert_report(report: dict) -> None:
    paraphrase = next(row for row in report["retrieval"] if row["id"] == "paraphrase-battery")
    if paraphrase["strategies"]["sparse"]["hit_at_3"]:
        raise AssertionError("关键词不应该独自找回改写问法的播放时间")
    if not paraphrase["strategies"]["rrf_hyde"]["hit_at_3"]:
        raise AssertionError("RRF+HyDE 应该找回播放时间")
    if not paraphrase["rerank_down"]["degraded"]:
        raise AssertionError("重排序关闭时必须留下降级标记")
    keyword = next(row for row in report["retrieval"] if row["id"] == "keyword-bluetooth")
    if not keyword["strategies"]["sparse"]["hit_at_3"]:
        raise AssertionError("原文词问法应该被关键词召回")
    hyde_draft = next(
        row
        for row in report["drafts"]
        if row["strategy"] == "rrf_hyde" and row["policy"] == "template-draft-conservative"
    )
    sparse_draft = next(
        row
        for row in report["drafts"]
        if row["strategy"] == "sparse" and row["policy"] == "template-draft-conservative"
    )
    if not hyde_draft["has_expected"] or hyde_draft["forbidden_hit"]:
        raise AssertionError("HyDE 草稿应写上 30 小时，且不能带禁用说法")
    if sparse_draft["has_expected"]:
        raise AssertionError("只有关键词时，这篇草稿不该出现 30 小时")
    if report["usage"]["cost"] != 0:
        raise AssertionError("离线对照的费用必须是 0")
    if any(not item["pass"] for item in report["workflow"]):
        raise AssertionError(f"流程回归没过：{report['workflow']}")
    resolved = resolve_product("掌柜示例耳机 X1 Pro 有没有主动降噪")
    if resolved["sku"] != "zg-ear-x1-pro":
        raise AssertionError("X1 Pro 不能被当成 X1")
    completion = report["completion"]
    if completion["population"] != "frozen draft+workflow":
        raise AssertionError("完成率只能统计冻结集里的草稿题和流程题")
    if completion["point_min"] != 1:
        raise AssertionError("标准要点的下限必须是 1")
    before_rows = completion["tasks"]["before"]
    after_rows = completion["tasks"]["after"]
    if [row["id"] for row in before_rows] != [row["id"] for row in after_rows]:
        raise AssertionError("前后两套配置必须跑同一批题")
    if completion["total"] != len(before_rows) or completion["total"] != len(after_rows):
        raise AssertionError("完成率分母必须等于这批题的条数")
    if any(not isinstance(row["success"], bool) for row in before_rows + after_rows):
        raise AssertionError("完成率必须是成功或失败")
    if completion["before"] != f"{sum(row['success'] for row in before_rows)}/{completion['total']}":
        raise AssertionError("Before 完成率分子和逐题结果对不上")
    if completion["after"] != f"{sum(row['success'] for row in after_rows)}/{completion['total']}":
        raise AssertionError("After 完成率分子和逐题结果对不上")


def main() -> None:
    parser = argparse.ArgumentParser(description="跑 Listing 固定任务对照")
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args()
    report = run_eval()
    assert_report(report)
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_baseline(report, BASELINE_PATH)
    print(report["conclusion"])
    print(f"报告已写入 {path}")
    print(f"基线表已写入 {BASELINE_PATH}")


if __name__ == "__main__":
    main()
