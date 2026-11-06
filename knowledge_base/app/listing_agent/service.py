"""对网页和测试暴露的 Listing 操作：开始、确认、拒绝、取消、恢复、核对。"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langgraph.types import Command

from app.listing_agent.catalog import display_name, get_product
from app.listing_agent.checkpoints import CheckpointStore
from app.listing_agent.ledger import Ledger
from app.listing_agent.paths import runtime_dir
from app.listing_agent.rules import Actor, validate_listing_draft
from app.listing_agent.trace import write_trace
from app.listing_agent.workflow import build_graph


logger = logging.getLogger("listing_agent.service")


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
    def __init__(self, runtime: Path | None = None):
        self.runtime = Path(runtime or runtime_dir())
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(self.runtime / "ledger.sqlite3")
        self.checkpoints = CheckpointStore(self.runtime / "checkpoints.pkl")
        self.graph = build_graph(self.checkpoints.saver, self.ledger)

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self.ledger.close()
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
    ) -> dict:
        query = (query or "").strip()
        if not query:
            raise ListingError("先写你要做的商品和平台")
        if role not in {"editor", "viewer"}:
            raise ListingError("身份只能是编辑或查看")
        if idempotency_key:
            existing = self.ledger.find_task_by_key(idempotency_key)
            if existing:
                return self.snapshot(existing)

        task_id = uuid.uuid4().hex
        session_id = session_id or uuid.uuid4().hex
        self.ledger.create_task(
            {
                "task_id": task_id,
                "session_id": session_id,
                "idempotency_key": idempotency_key or task_id,
                "user_id": user_id,
                "role": role,
                "platform": platform,
                "query": query,
            }
        )
        history = self.ledger.history_texts(session_id)
        self._invoke(
            task_id,
            {
                "task_id": task_id,
                "session_id": session_id,
                "user_id": user_id,
                "role": role,
                "platform": platform,
                "query": query,
                "history": history,
                "strategy": strategy,
                "rerank_mode": rerank_mode,
                "draft_policy": "template-draft-conservative",
                "inject_write_unknown": bool(inject_write_unknown),
                "skip_confirmation": bool(skip_confirmation),
                "skip_rules": bool(skip_rules),
            },
        )
        current = self.snapshot(task_id)
        self.ledger.append_message(session_id, query, current.get("sku") or "")
        return self.snapshot(task_id)

    def confirm(self, task_id: str, user_id: str, role: str, draft_version: int) -> dict:
        row = self._row(task_id)
        self._ensure_waiting(row)
        if role != "editor" or user_id != row["user_id"]:
            raise ListingError("只有创建这条任务的编辑可以确认写入", 403)
        if int(draft_version) != int(row["draft_version"] or 0):
            raise ListingError("草稿版本已经变了，请刷新后再确认")
        validation = _loads(row["validation_json"], {})
        if not validation.get("ok"):
            raise ListingError("规则没通过，不能写入")
        draft = _loads(row["draft_json"], {})
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
        row = self._row(task_id)
        self._ensure_owner_editor(row, user_id, role)
        self._ensure_waiting(row)
        self._invoke(
            task_id,
            Command(resume={"action": "reject", "user_id": user_id, "role": role, "reason": reason}),
        )
        return self.snapshot(task_id)

    def cancel(self, task_id: str, user_id: str, role: str) -> dict:
        row = self._row(task_id)
        self._ensure_owner_editor(row, user_id, role)
        if row["status"] in {"written", "written_checked"}:
            raise ListingError("已经写入的草稿不能用取消来删除")
        self.ledger.update_task(task_id, cancelled=1)
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
        row = self._row(task_id)
        self._ensure_owner_editor(row, user_id, role)
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
        row = self._row(task_id)
        self._ensure_owner_editor(row, user_id, role)
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
            for _chunk in self.graph.stream(graph_input, config, stream_mode="updates"):
                pass
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
                        "detail": draft.get("policy") or "",
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
            "draft_policy": draft.get("policy", ""),
            "spans": [
                {"name": "retrieve_evidence", "duration_ms": retrieval.get("duration_ms", 0)},
                {"name": "generate_draft", "duration_ms": draft.get("duration_ms", 0)},
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0},
            "usage_note": "默认草稿器不调用付费模型，用量和费用记 0。",
        }
        try:
            write_trace(self.runtime, record)
        except Exception:
            logger.exception("trace 写入失败，task_id=%s", task_id)

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

    def _ensure_owner_editor(self, row: dict, user_id: str, role: str) -> None:
        if role != "editor" or user_id != row["user_id"]:
            raise ListingError("只有创建这条任务的编辑可以操作", 403)

    @staticmethod
    def _interrupt_list(state_snapshot) -> list:
        found = list(state_snapshot.interrupts or [])
        for task in state_snapshot.tasks or []:
            found.extend(list(getattr(task, "interrupts", ()) or []))
        return found
