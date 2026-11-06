"""对网页和测试暴露的 Listing 操作：开始、确认、拒绝、取消、恢复、核对。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langgraph.types import Command

from app.listing_agent.catalog import display_name, get_product
from app.listing_agent.checkpoints import CheckpointStore
from app.listing_agent.ledger import IdempotencyConflict, Ledger
from app.listing_agent.llm_draft import append_telemetry, resolve_draft_mode
from app.listing_agent.paths import runtime_dir
from app.listing_agent.rules import Actor, validate_listing_draft
from app.listing_agent.trace import write_trace
from app.listing_agent.workflow import build_graph


logger = logging.getLogger("listing_agent.service")

_NODE_LABELS = {
    "resolve_product": "确认商品",
    "retrieve_evidence": "资料检索",
    "check_evidence": "证据核对",
    "generate_draft": "草稿生成",
    "validate_draft": "规则校验",
    "wait_human": "等待确认",
    "write_listing": "草稿写入",
    "finish": "结束",
}


def _stream_detail(update) -> str:
    if not isinstance(update, dict):
        return ""
    draft = update.get("draft") or {}
    if isinstance(draft, dict) and (draft.get("fallback_note") or draft.get("policy")):
        return draft.get("fallback_note") or draft.get("policy") or ""
    if update.get("sku"):
        return str(update.get("sku"))
    if update.get("status"):
        return str(update.get("status"))
    return ""


def _trace_usage(telemetry: dict) -> tuple[dict, str]:
    """离线路径的 0 表示没有调用模型。模型路径缺用量时保持 null。"""
    if telemetry.get("mode") != "llm":
        return (
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost": 0,
                "usage_known": True,
                "usage_source": "not_called",
            },
            "默认草稿器不调用付费模型，用量和费用记 0。",
        )
    return (
        {
            "prompt_tokens": telemetry.get("prompt_tokens"),
            "completion_tokens": telemetry.get("completion_tokens"),
            "total_tokens": telemetry.get("total_tokens"),
            "cost": telemetry.get("cost"),
            "usage_known": telemetry.get("usage_known"),
            "usage_source": telemetry.get("usage_source"),
        },
        telemetry.get("usage_note") or "",
    )


class ListingError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _loads(text: str | None, fallback):
    if not text:
        return fallback
    return json.loads(text)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ListingService:
    def __init__(self, runtime: Path | None = None, llm_client=None, llm_clients=None):
        self.runtime = Path(runtime or runtime_dir())
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.llm_client = llm_client
        self.llm_clients = llm_clients
        self.ledger = Ledger(self.runtime / "ledger.sqlite3")
        self.checkpoints = CheckpointStore(self.runtime / "checkpoints.pkl")
        self.graph = build_graph(
            self.checkpoints.saver,
            self.ledger,
            llm_client=llm_client,
            llm_clients=llm_clients,
        )

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self.ledger.close()
        self.checkpoints.close()
        self._closed = True

    def start(
        self,
        query: str,
        user_id: str,
        role: str,
        platform: str = "amazon",
        session_id: str | None = None,
        idempotency_key: str | None = None,
        rerank_mode: str = "on",
        inject_write_unknown: bool = False,
        strategy: str = "rrf_hyde",
        skip_confirmation: bool = False,
        skip_rules: bool = False,
        org_id: str = "",
        draft_mode: str | None = None,
    ) -> dict:
        query = (query or "").strip()
        if not query:
            raise ListingError("先写你要做的商品和平台")
        if role not in {"editor", "viewer"}:
            raise ListingError("身份只能是编辑或查看")
        try:
            mode = resolve_draft_mode(draft_mode)
        except ValueError as exc:
            raise ListingError(str(exc)) from exc
        org_id = org_id or ""
        task_id = uuid.uuid4().hex
        if session_id:
            try:
                self.ledger.claim_session(session_id, user_id, org_id)
            except PermissionError as exc:
                raise ListingError("没有这条会话", 404) from exc
        session_for_task = session_id or uuid.uuid4().hex
        scoped_key = idempotency_key or task_id
        task_row = {
            "task_id": task_id,
            "session_id": session_for_task,
            "idempotency_key": f"{org_id}|{user_id}|start_task|{scoped_key}",
            "user_id": user_id,
            "role": role,
            "platform": platform,
            "query": query,
            "org_id": org_id,
        }
        if idempotency_key:
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "query": query,
                        "platform": platform,
                        "session_id": session_id or "",
                        "draft_mode": mode,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            try:
                existing = self.ledger.reserve_idempotency(
                    org_id, user_id, "start_task", idempotency_key, digest, task_row
                )
            except IdempotencyConflict as exc:
                raise ListingError(str(exc), 409) from exc
            if existing:
                return self.snapshot_for(existing, user_id)
        else:
            self.ledger.create_task(task_row)
        if not session_id:
            self.ledger.claim_session(session_for_task, user_id, org_id)
        history = self.ledger.history_texts(session_for_task)
        self._invoke(
            task_id,
            {
                "task_id": task_id,
                "session_id": session_for_task,
                "user_id": user_id,
                "role": role,
                "platform": platform,
                "query": query,
                "history": history,
                "strategy": strategy,
                "rerank_mode": rerank_mode,
                "draft_policy": "template-draft-conservative",
                "draft_mode": mode,
                "inject_write_unknown": bool(inject_write_unknown),
                "skip_confirmation": bool(skip_confirmation),
                "skip_rules": bool(skip_rules),
            },
        )
        current = self.snapshot(task_id)
        self.ledger.append_message(session_for_task, query, current.get("sku") or "")
        return self.snapshot(task_id)

    def confirm(self, task_id: str, user_id: str, role: str, draft_version: int) -> dict:
        row = self._owned_row(task_id, user_id)
        self._ensure_waiting(row)
        if role != "editor":
            raise ListingError("当前身份不能确认写入", 403)
        if int(draft_version) != int(row["draft_version"] or 0):
            raise ListingError("草稿版本已经变了，请刷新后再确认")
        validation = _loads(row["validation_json"], {})
        if not validation.get("ok"):
            raise ListingError("规则没通过，不能写入")
        draft = _loads(row["draft_json"], {})
        state_snapshot = self.graph.get_state({"configurable": {"thread_id": task_id}})
        if not self._interrupt_list(state_snapshot):
            raise ListingError("检查点里没有等待确认的步骤，不能写入")
        self._invoke(
            task_id,
            Command(
                resume={
                    "action": "confirm",
                    "user_id": user_id,
                    "role": role,
                    "draft": draft,
                    "draft_version": int(draft_version),
                }
            ),
        )
        return self.snapshot(task_id)

    def reject(self, task_id: str, user_id: str, role: str, reason: str = "") -> dict:
        row = self._owned_row(task_id, user_id)
        self._ensure_editor(role)
        self._ensure_waiting(row)
        self._invoke(
            task_id,
            Command(resume={"action": "reject", "user_id": user_id, "role": role, "reason": reason}),
        )
        return self.snapshot(task_id)

    def cancel(self, task_id: str, user_id: str, role: str) -> dict:
        row = self._owned_row(task_id, user_id)
        self._ensure_editor(role)
        if row["status"] in {"written", "written_checked"}:
            raise ListingError("已经写入的草稿不能用取消来删除")
        self.ledger.update_task(task_id, cancelled=1)
        self._add_event(
            task_id,
            "node",
            {
                "name": "cancel",
                "label": "取消",
                "phase": "request",
                "detail": "取消已记下。断开页面推送不会走到这里。",
            },
        )
        if row["status"] == "awaiting_confirmation":
            self._invoke(
                task_id,
                Command(resume={"action": "cancel", "user_id": user_id, "role": role}),
            )
        else:
            self.ledger.update_task(task_id, status="cancelled")
        return self.snapshot(task_id)

    def update_draft(
        self,
        task_id: str,
        user_id: str,
        role: str,
        base_version: int,
        title: str,
        bullets: list[str],
    ) -> dict:
        row = self._owned_row(task_id, user_id)
        self._ensure_editor(role)
        self._ensure_waiting(row)
        if int(base_version) != int(row["draft_version"] or 0):
            raise ListingError("草稿版本已经变了，请刷新后再改")
        draft = dict(_loads(row["draft_json"], {}))
        draft["title"] = (title or "").strip()
        draft["bullets"] = [item.strip() for item in bullets if str(item).strip()]
        validation = validate_listing_draft(draft, row["platform"], Actor(user_id, role))
        version = int(base_version) + 1
        self.ledger.update_task(
            task_id,
            draft_json=json.dumps(draft, ensure_ascii=False),
            draft_version=version,
            validation_json=json.dumps(validation, ensure_ascii=False),
        )
        self._add_event(
            task_id,
            "tool",
            {
                "name": "validate_listing_draft",
                "phase": "edit",
                "ok": validation["ok"],
                "detail": "规则通过" if validation["ok"] else "；".join(validation["errors"]),
            },
        )
        return self.snapshot(task_id)

    def reconcile(self, task_id: str, user_id: str, role: str, found: bool) -> dict:
        row = self._owned_row(task_id, user_id)
        self._ensure_editor(role)
        if row["status"] != "write_unknown":
            raise ListingError("只有写入结果不明时才需要人工核对")
        version = int(row["draft_version"] or 0)
        key = f"{task_id}:{version}:write"
        draft = _loads(row["draft_json"], {})
        if found:
            receipt = {
                "status": "checked_present",
                "listing_id": "",
                "message": "人工核对：业务侧已经有这份草稿，台账不再写第二遍。",
            }
            self.ledger.replace_receipt(key, receipt, None)
            status = "written_checked"
        else:
            listing_id = f"lst-{task_id[:8]}-v{version}-retry"
            receipt = {
                "status": "written",
                "listing_id": listing_id,
                "message": "人工核对后确认当时没写上，已补写一次。",
            }
            self.ledger.replace_receipt(
                key,
                receipt,
                {
                    "listing_id": listing_id,
                    "task_id": task_id,
                    "sku": draft.get("sku") or "",
                    "version": version,
                    "body": draft,
                },
            )
            status = "written"
        self.ledger.update_task(
            task_id,
            status=status,
            receipt_json=json.dumps(receipt, ensure_ascii=False),
        )
        self._add_event(
            task_id,
            "final",
            {"name": "reconcile", "phase": "end", "status": status, "detail": receipt["message"]},
        )
        return self.snapshot(task_id)

    def snapshot(self, task_id: str) -> dict:
        row = self._row(task_id)
        product = get_product(row["sku"]) if row["sku"] else None
        draft = _loads(row["draft_json"], None)
        validation = _loads(row["validation_json"], None)
        retrieval = _loads(row["retrieval_json"], None)
        evidence = _loads(row["evidence_json"], [])
        receipt = _loads(row["receipt_json"], None)
        return {
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "role": row["role"],
            "platform": row["platform"],
            "query": row["query"],
            "status": row["status"],
            "sku": row["sku"],
            "product_name": display_name(product) if product else "",
            "clarify_question": row["clarify_question"] or "",
            "candidates": _loads(row["candidates_json"], []),
            "draft": draft,
            "draft_version": int(row["draft_version"] or 0),
            "validation": validation,
            "retrieval": retrieval,
            "evidence": evidence,
            "receipt": receipt,
            "cancelled": bool(row["cancelled"]),
            "events": self.ledger.list_events(task_id),
        }

    def _invoke(self, task_id: str, graph_input) -> None:
        config = {"configurable": {"thread_id": task_id}}
        try:
            pause_node = os.getenv("LISTING_PAUSE_AFTER_NODE", "").strip()
            pause_seconds = float(os.getenv("LISTING_PAUSE_SECONDS", "0") or 0)
            for chunk in self.graph.stream(graph_input, config, stream_mode="updates"):
                if isinstance(chunk, dict):
                    for name, update in chunk.items():
                        self._add_event(
                            task_id,
                            "node",
                            {
                                "name": name,
                                "label": _NODE_LABELS.get(name, name),
                                "phase": "end",
                                "detail": _stream_detail(update),
                            },
                        )
                        if pause_node and name == pause_node and pause_seconds > 0:
                            time.sleep(pause_seconds)
            state_snapshot = self.graph.get_state(config)
            values = state_snapshot.values or {}
            interrupts = self._interrupt_list(state_snapshot)
            self._persist(task_id, values, interrupts)
        except Exception as exc:
            logger.exception("Listing 流程失败，task_id=%s", task_id)
            self.ledger.update_task(task_id, status="failed", clarify_question=str(exc))
            self._add_event(task_id, "error", {"name": "failed", "phase": "end", "detail": str(exc)})
            raise ListingError(f"流程没有跑完：{exc}") from exc
        finally:
            self.checkpoints.save()

    def _persist(self, task_id: str, values: dict, interrupts: list) -> None:
        status = values.get("status") or "finished"
        if interrupts:
            status = "awaiting_confirmation"
        draft = values.get("confirm_draft") or values.get("draft")
        validation = values.get("validation")
        retrieval = values.get("retrieval")
        evidence = values.get("evidence") or []
        receipt = values.get("write_receipt")
        version = values.get("confirm_version") or values.get("draft_version") or 0
        self.ledger.update_task(
            task_id,
            status=status,
            sku=values.get("sku") or "",
            clarify_question=values.get("clarify_question") or "",
            candidates_json=json.dumps(values.get("candidates") or [], ensure_ascii=False),
            draft_json=json.dumps(draft, ensure_ascii=False) if draft else "",
            draft_version=int(version),
            validation_json=json.dumps(validation, ensure_ascii=False) if validation else "",
            retrieval_json=json.dumps(retrieval, ensure_ascii=False) if retrieval else "",
            evidence_json=json.dumps(evidence, ensure_ascii=False),
            receipt_json=json.dumps(receipt, ensure_ascii=False) if receipt else "",
        )
        for event_name, data in self._timeline(values, interrupts):
            self._add_event(task_id, event_name, data)
        self._write_trace(task_id, values, status)

    def _timeline(self, values: dict, interrupts: list) -> list[tuple[str, dict]]:
        events = []
        status = values.get("status")
        if status in {"ambiguous", "insufficient"} and not values.get("draft") and not values.get("retrieval"):
            events.append(
                (
                    "node",
                    {
                        "name": "resolve_product",
                        "label": "确认商品",
                        "phase": "end",
                        "detail": values.get("clarify_question") or "",
                    },
                )
            )
            return events

        events.append(
            (
                "node",
                {
                    "name": "resolve_product",
                    "label": "确认商品",
                    "phase": "end",
                    "detail": values.get("sku") or "",
                },
            )
        )
        retrieval = values.get("retrieval") or {}
        if retrieval:
            detail = (
                f"策略 {retrieval.get('strategy')}，证据 {len(values.get('evidence') or [])} 条"
            )
            if retrieval.get("rerank_degraded"):
                detail += "；重排序不可用，已退回融合排序"
            events.append(
                (
                    "node",
                    {
                        "name": "retrieve_evidence",
                        "label": "资料检索",
                        "phase": "end",
                        "detail": detail,
                    },
                )
            )
            events.append(
                (
                    "tool",
                    {
                        "name": "rerank",
                        "phase": "end",
                        "ok": not retrieval.get("rerank_degraded"),
                        "detail": retrieval.get("rerank_error") or "重排序完成",
                    },
                )
            )
        if values.get("evidence") is not None and values.get("sku"):
            evidence_detail = "证据够用，可以写草稿" if values.get("evidence") else "证据不足"
            events.append(
                (
                    "node",
                    {
                        "name": "check_evidence",
                        "label": "证据核对",
                        "phase": "end",
                        "detail": evidence_detail,
                    },
                )
            )
        draft = values.get("draft")
        if draft:
            events.append(
                (
                    "node",
                    {
                        "name": "generate_draft",
                        "label": "草稿生成",
                        "phase": "end",
                        "detail": draft.get("fallback_note") or draft.get("policy") or "",
                    },
                )
            )
        elif values.get("status") == "draft_failed":
            events.append(
                (
                    "node",
                    {
                        "name": "generate_draft",
                        "label": "草稿生成",
                        "phase": "end",
                        "detail": values.get("clarify_question") or "草稿没有生成",
                    },
                )
            )
        validation = values.get("validation")
        if validation:
            events.append(
                (
                    "tool",
                    {
                        "name": "validate_listing_draft",
                        "phase": "end",
                        "ok": bool(validation.get("ok")),
                        "detail": "规则通过" if validation.get("ok") else "；".join(validation.get("errors") or []),
                    },
                )
            )
        if interrupts:
            events.append(
                (
                    "draft",
                    {
                        "name": "wait_human",
                        "label": "等待确认",
                        "phase": "pause",
                        "detail": "点页面上的「确认写入」才会进台账",
                        "version": values.get("draft_version") or 0,
                    },
                )
            )
        decision = values.get("decision")
        receipt = values.get("write_receipt") or {}
        if decision == "reject":
            events.append(
                ("final", {"name": "finish", "phase": "end", "status": "rejected", "detail": "已拒绝，没有写入"})
            )
        elif decision == "cancel":
            events.append(
                ("final", {"name": "finish", "phase": "end", "status": "cancelled", "detail": "已取消"})
            )
        elif receipt:
            events.append(
                (
                    "final",
                    {
                        "name": "write_listing",
                        "label": "草稿写入",
                        "phase": "end",
                        "status": receipt.get("status"),
                        "detail": receipt.get("message") or "",
                    },
                )
            )
        return events

    def _add_event(self, task_id: str, event: str, data: dict) -> None:
        key = f"{event}:{data.get('name')}:{data.get('phase', '')}:{data.get('status', '')}"
        for old in self.ledger.list_events(task_id):
            old_data = old["data"]
            old_key = (
                f"{old['event']}:{old_data.get('name')}:{old_data.get('phase', '')}:{old_data.get('status', '')}"
            )
            if old_key == key:
                return
        self.ledger.add_event(task_id, event, data)

    def _write_trace(self, task_id: str, values: dict, status: str) -> None:
        row = self.ledger.get_task_row(task_id) or {}
        retrieval = values.get("retrieval") or {}
        draft = values.get("draft") or {}
        telemetry = values.get("draft_telemetry") or {}
        usage, usage_note = _trace_usage(telemetry)
        record = {
            "trace_id": task_id,
            "ended_at": _now(),
            "user_id": row.get("user_id", ""),
            "session_id": row.get("session_id", ""),
            "query": row.get("query", ""),
            "status": status,
            "strategy": retrieval.get("strategy", ""),
            "embedding": retrieval.get("embedding", ""),
            "reranker": retrieval.get("reranker", ""),
            "draft_policy": draft.get("policy", "") or telemetry.get("mode", ""),
            "draft_mode": telemetry.get("mode", ""),
            "model": telemetry.get("model"),
            "model_name_source": telemetry.get("model_name_source"),
            "parameters": telemetry.get("parameters"),
            "prompt_version": telemetry.get("prompt_version"),
            "failure_type": telemetry.get("failure_type"),
            "spans": [
                {"name": "retrieve_evidence", "duration_ms": retrieval.get("duration_ms", 0)},
                {
                    "name": "generate_draft",
                    "duration_ms": telemetry.get("duration_ms", draft.get("duration_ms", 0)),
                },
            ],
            "usage": usage,
            "usage_note": usage_note,
        }
        try:
            write_trace(self.runtime, record)
            if telemetry:
                append_telemetry(self.runtime, {"trace_id": task_id, **telemetry})
        except Exception:
            logger.exception("trace 写入失败，task_id=%s", task_id)

    def snapshot_for(self, task_id: str, user_id: str) -> dict:
        self._owned_row(task_id, user_id)
        return self.snapshot(task_id)

    def _owned_row(self, task_id: str, user_id: str) -> dict:
        row = self.ledger.get_task_row(task_id)
        if row is None or row.get("user_id") != user_id:
            raise ListingError("没有这条任务", 404)
        return row

    def _ensure_editor(self, role: str) -> None:
        if role != "editor":
            raise ListingError("当前身份不能修改这条任务", 403)

    def _row(self, task_id: str) -> dict:
        row = self.ledger.get_task_row(task_id)
        if row is None:
            raise ListingError("没有这条任务", 404)
        return row

    def _ensure_waiting(self, row: dict) -> None:
        if row["cancelled"]:
            raise ListingError("任务已取消")
        if row["status"] != "awaiting_confirmation":
            raise ListingError("现在不是等待确认的状态")

    @staticmethod
    def _interrupt_list(state_snapshot) -> list:
        found = list(state_snapshot.interrupts or [])
        for task in state_snapshot.tasks or []:
            found.extend(list(getattr(task, "interrupts", ()) or []))
        return found
