"""资料检索 → 证据核对 → 草稿 → 人工确认 → 写入。

商品资料和规则是独立新增的演示样例。
RRF 的计算方式和问答检索一致。导入、问答两条流程不在这里改。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from app.listing_agent.catalog import chunks_for, display_name, get_product, resolve_product
from app.listing_agent.drafting import build_draft
from app.evidence.store import retrieve_product_evidence
from app.listing_agent.llm_draft import generate_draft_with_fallback, offline_telemetry, resolve_draft_mode
from app.listing_agent.rules import Actor, validate_listing_draft


class ListingState(TypedDict, total=False):
    task_id: str
    session_id: str
    user_id: str
    role: str
    platform: str
    query: str
    history: list
    strategy: str
    rerank_mode: str
    draft_policy: str
    draft_mode: str
    draft_telemetry: dict
    inject_write_unknown: bool
    skip_confirmation: bool
    skip_rules: bool
    sku: str
    candidates: list
    clarify_question: str
    resolve_via: str
    evidence: list
    retrieval: dict
    draft: dict
    draft_version: int
    validation: dict
    decision: str
    confirm_actor: str
    confirm_role: str
    confirm_draft: dict
    confirm_version: int
    write_receipt: dict
    status: str


def _candidate_names(skus: list[str]) -> list[str]:
    names = []
    for sku in skus:
        product = get_product(sku)
        names.append(display_name(product) if product else sku)
    return names


def _write(state: ListingState, ledger, shop_writer=None) -> dict:
    task_id = state["task_id"]
    owner = state.get("user_id") or ""
    actor_id = state.get("confirm_actor") or ""
    role = state.get("confirm_role") or ""
    draft = state.get("confirm_draft") or state.get("draft") or {}
    version = int(state.get("confirm_version") or 0)
    row = ledger.get_task_row(task_id)
    if row and row["cancelled"]:
        return {"write_receipt": {"status": "forbidden", "message": "任务已取消，不能写入"}}
    if role != "editor" or actor_id != owner:
        return {
            "write_receipt": {
                "status": "forbidden",
                "message": "只有创建这条任务的编辑可以确认写入",
            }
        }
    if not state.get("skip_confirmation") and (
        row is None or int(row["draft_version"] or 0) != version
    ):
        return {
            "write_receipt": {
                "status": "forbidden",
                "message": "草稿版本已经变了，请刷新后再确认",
            }
        }
    if state.get("skip_rules"):
        validation = {
            "ok": True,
            "skipped": True,
            "errors": [],
            "platform": state.get("platform") or "amazon",
        }
    else:
        try:
            validation = validate_listing_draft(
                draft, state.get("platform") or "amazon", Actor(actor_id, role)
            )
        except PermissionError as exc:
            return {"write_receipt": {"status": "forbidden", "message": str(exc)}}
    if not validation["ok"]:
        return {
            "write_receipt": {
                "status": "forbidden",
                "message": "；".join(validation["errors"]),
                "validation": validation,
            },
            "validation": validation,
        }

    if shop_writer is not None:
        if state.get("skip_confirmation") or state.get("skip_rules"):
            return {"write_receipt": {"status": "forbidden", "message": "真实店铺写入不允许跳过人工确认或规则校验"}}
        receipt = shop_writer.write(task_id, version, draft)
        return {"write_receipt": receipt, "validation": validation}

    key = f"{task_id}:{version}:write"
    if state.get("inject_write_unknown"):
        receipt = {
            "status": "unknown",
            "listing_id": "",
            "message": "写入工具没有返回成功或失败。请人工核对，不要直接再写一次。",
        }
        stored = ledger.save_receipt_and_maybe_listing(key, task_id, receipt, None)
        return {"write_receipt": stored, "validation": validation}

    listing_id = f"lst-{task_id[:8]}-v{version}"
    receipt = {"status": "written", "listing_id": listing_id, "message": "草稿已写入台账"}
    listing = {
        "listing_id": listing_id,
        "task_id": task_id,
        "sku": draft.get("sku") or "",
        "version": version,
        "body": draft,
    }
    stored = ledger.save_receipt_and_maybe_listing(key, task_id, receipt, listing)
    return {"write_receipt": stored, "validation": validation}


def build_graph(checkpointer, ledger, llm_client=None, llm_clients=None, shop_writer=None):
    """编译带检查点的 Listing 图。ledger 用来做写入幂等。

    llm_client 只在 draft_mode=llm 时使用。不传就按环境变量连接真实模型。
    """

    def resolve_node(state: ListingState) -> dict:
        found = resolve_product(state.get("query") or "", state.get("history") or [])
        if found["kind"] == "one":
            return {
                "sku": found["sku"],
                "status": "resolved",
                "candidates": found["candidates"],
                "clarify_question": "",
                "resolve_via": found["via"],
            }
        if found["kind"] == "many":
            names = _candidate_names(found["candidates"])
            question = "你说的是哪一个：" + "，还是".join(names) + "？请点明型号后再写。"
            return {
                "sku": "",
                "status": "ambiguous",
                "candidates": found["candidates"],
                "clarify_question": question,
                "resolve_via": found["via"],
            }
        return {
            "sku": "",
            "status": "insufficient",
            "candidates": [],
            "clarify_question": "资料里没有这个商品，我不能编一份草稿。",
            "resolve_via": found["via"],
        }

    def retrieve_node(state: ListingState) -> dict:
        result = retrieve_product_evidence(
            state.get("query") or "",
            sku=state.get("sku") or None,
            strategy=state.get("strategy") or "rrf_hyde",
            rerank_mode=state.get("rerank_mode") or "on",
        )
        # 型号已经确定、但这句话没再提参数时，用整份商品资料做证据。
        if not result["evidence"] and state.get("sku"):
            sheet = chunks_for(state["sku"])[:5]
            for row in sheet:
                row["score"] = 0.01
                row["chunk_id"] = row["id"]
                row["source"] = f"listing_data/products/{state['sku']}.json#{row['id']}"
                row["sku"] = state["sku"]
                row["product_id"] = state["sku"]
            result["evidence"] = sheet
            result["meta"]["fallback"] = "whole_product_sheet"
        return {"evidence": result["evidence"], "retrieval": result["meta"]}

    def check_node(state: ListingState) -> dict:
        if state.get("evidence"):
            return {"status": "evidence_ok"}
        return {
            "status": "insufficient",
            "clarify_question": "找到了型号，但没有对得上的资料，草稿先不写。",
        }

    def draft_node(state: ListingState) -> dict:
        try:
            mode = resolve_draft_mode(state.get("draft_mode") or "offline")
        except ValueError as exc:
            return {
                "status": "draft_failed",
                "clarify_question": str(exc),
                "draft_telemetry": {
                    "mode": "llm",
                    "failure_type": "transport_error",
                    "usage_known": False,
                    "cost": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "message": str(exc),
                },
            }
        if mode == "llm":
            # 模型失败会换另一家，再不行用知识库原句。规则和人工确认仍在后面。
            outcome = generate_draft_with_fallback(
                state.get("query") or "",
                state.get("sku") or "",
                state.get("evidence") or [],
                state.get("platform") or "amazon",
                client=llm_client,
                clients=llm_clients,
            )
            if outcome.draft is None:
                return {
                    "status": "draft_failed",
                    "clarify_question": outcome.message,
                    "draft_telemetry": outcome.telemetry,
                }
            return {
                "draft": outcome.draft,
                "draft_version": 1,
                "status": "drafted",
                "draft_telemetry": outcome.telemetry,
            }
        draft = build_draft(
            state.get("query") or "",
            state.get("sku") or "",
            state.get("evidence") or [],
            state.get("draft_policy") or "template-draft-conservative",
        )
        draft["platform"] = state.get("platform") or "amazon"
        return {
            "draft": draft,
            "draft_version": 1,
            "status": "drafted",
            "draft_telemetry": offline_telemetry(draft["duration_ms"]),
        }

    def validate_node(state: ListingState) -> dict:
        if state.get("skip_rules"):
            return {
                "validation": {
                    "ok": True,
                    "skipped": True,
                    "errors": [],
                    "platform": state.get("platform") or "amazon",
                },
                "status": "checked",
            }
        validation = validate_listing_draft(
            state.get("draft") or {},
            state.get("platform") or "amazon",
            Actor(state.get("user_id") or "", state.get("role") or "viewer"),
        )
        return {"validation": validation, "status": "checked"}

    def wait_node(state: ListingState) -> dict:
        if state.get("skip_confirmation"):
            draft = state.get("draft") or {}
            return {
                "decision": "confirm",
                "confirm_actor": state.get("user_id") or "",
                "confirm_role": state.get("role") or "",
                "confirm_draft": draft,
                "confirm_version": int(state.get("draft_version") or 0),
            }
        decision: Any = interrupt(
            {
                "task_id": state.get("task_id"),
                "draft": state.get("draft"),
                "draft_version": state.get("draft_version"),
                "validation": state.get("validation"),
            }
        )
        if not isinstance(decision, dict):
            decision = {"action": "reject"}
        return {
            "decision": decision.get("action") or "reject",
            "confirm_actor": decision.get("user_id") or "",
            "confirm_role": decision.get("role") or "",
            "confirm_draft": decision.get("draft") or state.get("draft") or {},
            "confirm_version": int(decision.get("draft_version") or state.get("draft_version") or 0),
        }

    def write_node(state: ListingState) -> dict:
        return _write(state, ledger, shop_writer)

    def finish_node(state: ListingState) -> dict:
        receipt = state.get("write_receipt") or {}
        status = receipt.get("status")
        if status == "written":
            return {"status": "written"}
        if status == "unknown":
            return {"status": "write_unknown"}
        if status == "conflict":
            return {"status": "write_conflict"}
        if status == "failed":
            return {"status": "write_failed"}
        if status == "forbidden":
            return {"status": "confirm_rejected"}
        if state.get("decision") == "reject":
            return {"status": "rejected"}
        if state.get("decision") == "cancel":
            return {"status": "cancelled"}
        return {"status": state.get("status") or "finished"}

    def after_resolve(state: ListingState) -> str:
        if state.get("status") == "resolved":
            return "retrieve_evidence"
        return "finish"

    def after_check(state: ListingState) -> str:
        if state.get("status") == "evidence_ok":
            return "generate_draft"
        return "finish"

    def after_draft(state: ListingState) -> str:
        if state.get("status") == "drafted" and state.get("draft"):
            return "validate_draft"
        return "finish"

    def after_wait(state: ListingState) -> str:
        if state.get("decision") == "confirm":
            return "write_listing"
        return "finish"

    graph = StateGraph(ListingState)
    graph.add_node("resolve_product", resolve_node)
    graph.add_node("retrieve_evidence", retrieve_node)
    graph.add_node("check_evidence", check_node)
    graph.add_node("generate_draft", draft_node)
    graph.add_node("validate_draft", validate_node)
    graph.add_node("wait_human", wait_node)
    graph.add_node("write_listing", write_node)
    graph.add_node("finish", finish_node)
    graph.add_edge(START, "resolve_product")
    graph.add_conditional_edges(
        "resolve_product",
        after_resolve,
        {"retrieve_evidence": "retrieve_evidence", "finish": "finish"},
    )
    graph.add_edge("retrieve_evidence", "check_evidence")
    graph.add_conditional_edges(
        "check_evidence",
        after_check,
        {"generate_draft": "generate_draft", "finish": "finish"},
    )
    graph.add_conditional_edges(
        "generate_draft",
        after_draft,
        {"validate_draft": "validate_draft", "finish": "finish"},
    )
    graph.add_edge("validate_draft", "wait_human")
    graph.add_conditional_edges(
        "wait_human",
        after_wait,
        {"write_listing": "write_listing", "finish": "finish"},
    )
    graph.add_edge("write_listing", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)
