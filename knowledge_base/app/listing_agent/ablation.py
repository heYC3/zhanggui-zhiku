"""留出案例上的分项对照。

冻结测集不动。这里的台灯和插线板没有参与原来的模板调优。
开发族不写入对外数字。检索、确认门、规则、草稿器分开记，不再合成一个前后对比。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.listing_agent.paths import DATA_DIR

HELD_DIR = DATA_DIR / "heldout"
PRODUCT_DIR = HELD_DIR / "products"
CASES_PATH = HELD_DIR / "cases.json"
REPORT_PATH = DATA_DIR / "reports" / "ablation.md"


def load_products() -> list[dict]:
    products = []
    for path in sorted(PRODUCT_DIR.glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        products.append(item)
    return products


def load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def _chunks(product: dict) -> list[dict]:
    rows = []
    for chunk in product["chunks"]:
        rows.append({**chunk, "sku": product["sku"], "text": chunk["text"]})
    return rows


def resolve(query: str, products: list[dict]) -> dict:
    unique = []
    shared = []
    for product in products:
        for alias in product.get("unique_aliases") or []:
            if alias in query:
                unique.append((len(alias), product["sku"]))
        for alias in product.get("shared_aliases") or []:
            if alias in query:
                shared.append((len(alias), product["sku"]))
    if unique:
        best = max(length for length, _sku in unique)
        skus = []
        for length, sku in unique:
            if length == best and sku not in skus:
                skus.append(sku)
        if len(skus) == 1:
            return {"kind": "one", "sku": skus[0]}
        return {"kind": "many", "sku": ""}
    skus = []
    for _length, sku in shared:
        if sku not in skus:
            skus.append(sku)
    if len(skus) == 1:
        return {"kind": "one", "sku": skus[0]}
    if len(skus) > 1:
        return {"kind": "many", "sku": ""}
    return {"kind": "none", "sku": ""}


def _focus_terms(query: str, product: dict) -> set[str]:
    from app.listing_agent.retrieve import terms_of

    focus = query
    aliases = list(product.get("unique_aliases") or []) + list(product.get("shared_aliases") or [])
    for alias in sorted(aliases, key=len, reverse=True):
        focus = focus.replace(alias, " ")
    return set(terms_of(focus)) or set(terms_of(query))


def _rank_score(row: dict, focus_terms: set[str]) -> int:
    from app.listing_agent.retrieve import terms_of

    return len(focus_terms & set(terms_of(row["text"])))


def search(query: str, sku: str, products: list[dict]) -> list[dict]:
    """按问句和原句的共同词排序。型号名已经用来选定商品，不再拿来抢名次。"""
    product = next(item for item in products if item["sku"] == sku)
    focus_terms = _focus_terms(query, product)
    rows = _chunks(product)
    return sorted(rows, key=lambda row: _rank_score(row, focus_terms), reverse=True)


def _template_draft(product: dict, evidence: list[dict]) -> dict:
    lead = evidence[0]["text"].strip()
    title = f"{product['brand']}{product['model']} {lead}"
    if len(title) > 80:
        title = title[:80]
    bullets = []
    for row in evidence:
        sentence = row["text"].strip()
        if sentence and sentence not in bullets:
            bullets.append(sentence)
    return {
        "title": title,
        "bullets": bullets,
        "brand": product["brand"],
        "model": product["model"],
        "sku": product["sku"],
    }


def _text(draft: dict | None) -> str:
    if not draft:
        return ""
    return (draft.get("title") or "") + "\n" + "\n".join(draft.get("bullets") or [])


def score_case(case: dict, products: list[dict]) -> dict:
    found = resolve(case["query"], products)
    retrieval_hit = False
    evidence = []
    if found["kind"] == "one":
        evidence = search(case["query"], found["sku"], products)
        retrieval_hit = bool(evidence) and evidence[0]["id"] == case["expect_chunk"]
    gate_stops = found["kind"] != "one"
    draft = None
    product = None
    if found["kind"] == "one" and evidence:
        product = next(item for item in products if item["sku"] == found["sku"])
        best = _rank_score(evidence[0], _focus_terms(case["query"], product))
        lead = [row for row in evidence if _rank_score(row, _focus_terms(case["query"], product)) == best and best > 0]
        draft = _template_draft(product, lead or evidence[:1])
    text = _text(draft)
    evidence_text = "\n".join(row["text"] for row in evidence)
    forbidden = [word for word in case.get("forbid_in_text") or [] if word and word in text]
    rules_ok = False
    if draft is not None and product is not None:
        bullets = draft["bullets"]
        rules_ok = (
            bool(bullets)
            and all(item in evidence_text for item in bullets)
            and draft["brand"] == product["brand"]
            and draft["model"] == product["model"]
            and not forbidden
        )
    expect = case.get("expect_in_text")
    any_ok = case.get("expect_any") or []
    covered = (expect in text) if expect else any(word in text for word in any_ok)
    return {
        "id": case["id"],
        "题型": case["题型"],
        "检索命中": retrieval_hit,
        "确认门会停": gate_stops,
        "草稿覆盖": bool(draft) and covered and not forbidden,
        "规则通过": rules_ok,
        "禁用词": forbidden,
        "说明": case["purpose"],
    }


def probe_live_fallbacks() -> list[str]:
    """真实组件能跑就记结果，失败就记下退回的路径。不把失败写成成功。"""
    lines = []
    import os

    os.environ["LISTING_USE_BGE"] = "1"
    try:
        from app.listing_agent.retrieve import search_evidence

        found = search_evidence("掌柜示例耳机 X1 的蓝牙版本", sku="zg-ear-x1", strategy="rrf_hyde")
        embedding = found["meta"].get("embedding")
        error = found["meta"].get("bge_error") or ""
        if embedding == "BAAI/bge-m3" and not error:
            lines.append(f"- 真实 BGE：这次检索用的是 {embedding}。")
        else:
            lines.append(f"- 真实 BGE 没有用上，已退回离线字块。记录：{error or embedding}")
    except Exception as exc:
        lines.append(f"- 真实 BGE 调用失败，检索仍退回离线字块。原因：{exc}")
    finally:
        os.environ.pop("LISTING_USE_BGE", None)

    try:
        from app.lm.reranker_http_utils import rerank_documents

        scores = rerank_documents("蓝牙版本", ["蓝牙版本是 5.3，打开盒盖就能和手机配对。", "今天天气不错。"])
        if scores and scores[0] >= scores[-1]:
            lines.append(f"- 真实重排：qwen3-rerank 返回了 {len(scores)} 个分。相关句不低于无关句。")
        else:
            lines.append(f"- 真实重排有返回，但顺序异常：{scores}。Listing 默认仍用本地词重叠。")
    except Exception as exc:
        lines.append(f"- 真实重排失败，Listing 继续用本地词重叠。原因：{exc}")

    try:
        from app.listing_agent.llm_draft import DraftCallError, OpenAICompatibleClient, generate_draft_with_fallback
        from app.listing_agent.retrieve import search_evidence

        evidence = search_evidence("掌柜示例耳机 X1 的蓝牙版本", sku="zg-ear-x1", strategy="sparse")["evidence"]

        class _Down:
            def complete(self, *, messages, timeout_s):
                raise DraftCallError("timeout", "用来演示的超时")

        failed = generate_draft_with_fallback(
            "掌柜示例耳机 X1 的蓝牙版本，写成要点",
            "zg-ear-x1",
            evidence,
            "amazon",
            clients={"bailian": _Down(), "openai": _Down()},
            allow_env=False,
        )
        lines.append(
            "- 两家模型都超时：草稿改用知识库原句。"
            f" 来源 {failed.telemetry.get('provider')}，费用 {failed.telemetry.get('cost')}。"
        )
        client = OpenAICompatibleClient.from_bailian_env()
        live = generate_draft_with_fallback(
            "掌柜留出台灯不在主目录",
            "zg-ear-x1",
            evidence,
            "amazon",
            clients={"bailian": client, "openai": _Down()},
            allow_env=False,
        )
        if live.draft is None:
            lines.append(f"- 真实百炼草稿没有生成。原因：{live.message}")
        else:
            lines.append(
                f"- 真实百炼草稿模型是 {live.telemetry.get('model')}，"
                f"耗时 {live.telemetry.get('duration_ms')} 毫秒，"
                f"token {live.telemetry.get('prompt_tokens')}+{live.telemetry.get('completion_tokens')}。"
            )
    except Exception as exc:
        lines.append(f"- 真实模型草稿没有跑完，失败后仍应退回知识库原句。原因：{exc}")
    return lines


def write_report(rows: list[dict], live_lines: list[str] | None = None) -> Path:
    test_rows = [row for row in rows if row.get("split") == "test"]
    lines = [
        "# 留出案例分项对照",
        "",
        "冻结测集没有改。下面的台灯、插线板没有参与原来 3 个商品的模板调优。",
        "开发族不在这张表里。检索、确认门、规则、草稿器分开看。",
        "",
        "| 题 | 题型 | 检索命中 | 确认门会停 | 草稿覆盖且无禁用词 | 规则通过 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in test_rows:
        lines.append(
            "| {id} | {题型} | {hit} | {gate} | {draft} | {rules} |".format(
                id=row["id"],
                题型=row["题型"],
                hit="是" if row["检索命中"] else "否",
                gate="是" if row["确认门会停"] else "否",
                draft="是" if row["草稿覆盖"] else "否",
                rules="是" if row["规则通过"] else "否",
            )
        )
    lines.extend(
        [
            "",
            "## 每题在看什么",
            "",
        ]
    )
    for row in test_rows:
        lines.append(f"- {row['id']}：{row['说明']}")
    lines.extend(["", "## 失败时退回哪里", ""])
    lines.extend(
        live_lines
        or [
            "- 真实 BGE、真实重排和真实模型的这次记录还没有跑。离线检索失败时退回词重叠，模型都失败时退回知识库原句。",
        ]
    )
    lines.append("")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return REPORT_PATH


def run(live: bool = False) -> dict:
    products = load_products()
    cases = load_cases()
    rows = []
    for case in cases:
        if case["split"] != "test":
            continue
        row = score_case(case, products)
        row["split"] = "test"
        rows.append(row)
    live_lines = probe_live_fallbacks() if live else None
    path = write_report(rows, live_lines)
    return {"rows": rows, "report": str(path)}


def main() -> None:
    result = run(live=True)
    print(f"分项对照已写入 {result['report']}")


if __name__ == "__main__":
    main()
