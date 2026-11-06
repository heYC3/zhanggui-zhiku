"""Listing 网页接口。导入和问答还在原来的 8000、8001，这里用 8002。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.listing_agent.paths import MODEL_CARD_PATH
from app.listing_agent.service import ListingError, ListingService


app = FastAPI(title="掌柜智库 Listing", description="商品资料检索、草稿确认和写入")
_service: ListingService | None = None

_TERMINAL = {
    "written",
    "written_checked",
    "rejected",
    "cancelled",
    "insufficient",
    "ambiguous",
    "failed",
    "confirm_rejected",
    "write_unknown",
}


def get_service() -> ListingService:
    global _service
    if _service is None:
        _service = ListingService()
    return _service


@app.exception_handler(ListingError)
async def _listing_error(_request: Request, exc: ListingError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


class StartBody(BaseModel):
    query: str
    user_id: str = "小明"
    role: str = "editor"
    platform: str = "amazon"
    session_id: str | None = None
    idempotency_key: str | None = None
    rerank_mode: str = "on"
    inject_write_unknown: bool = False


class DecisionBody(BaseModel):
    user_id: str
    role: str = "editor"
    draft_version: int = 0
    reason: str = ""


class DraftBody(BaseModel):
    user_id: str
    role: str = "editor"
    base_version: int
    title: str = ""
    bullets: list[str] = Field(default_factory=list)


class ReconcileBody(BaseModel):
    user_id: str
    role: str = "editor"
    found: bool


@app.get("/listing.html")
async def listing_page():
    page = Path(__file__).resolve().parent / "page" / "listing.html"
    if not page.exists():
        return JSONResponse(status_code=404, content={"detail": "页面文件不见了"})
    return FileResponse(page)


@app.get("/api/listing/health")
async def health():
    return {"ok": True, "service": "listing-agent"}


@app.get("/api/listing/meta")
async def meta():
    card = json.loads(MODEL_CARD_PATH.read_text(encoding="utf-8"))
    return card


@app.post("/api/listing/tasks")
async def start_task(body: StartBody):
    return get_service().start(
        query=body.query,
        user_id=body.user_id.strip(),
        role=body.role,
        platform=body.platform,
        session_id=body.session_id,
        idempotency_key=body.idempotency_key,
        rerank_mode=body.rerank_mode,
        inject_write_unknown=body.inject_write_unknown,
    )


@app.get("/api/listing/tasks/{task_id}")
async def get_task(task_id: str):
    return get_service().snapshot(task_id)


@app.post("/api/listing/tasks/{task_id}/confirm")
async def confirm_task(task_id: str, body: DecisionBody):
    return get_service().confirm(task_id, body.user_id.strip(), body.role, body.draft_version)


@app.post("/api/listing/tasks/{task_id}/reject")
async def reject_task(task_id: str, body: DecisionBody):
    return get_service().reject(task_id, body.user_id.strip(), body.role, body.reason)


@app.post("/api/listing/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, body: DecisionBody):
    return get_service().cancel(task_id, body.user_id.strip(), body.role)


@app.post("/api/listing/tasks/{task_id}/draft")
async def edit_draft(task_id: str, body: DraftBody):
    return get_service().update_draft(
        task_id,
        body.user_id.strip(),
        body.role,
        body.base_version,
        body.title,
        body.bullets,
    )


@app.post("/api/listing/tasks/{task_id}/reconcile")
async def reconcile_task(task_id: str, body: ReconcileBody):
    return get_service().reconcile(task_id, body.user_id.strip(), body.role, body.found)


@app.get("/api/listing/stream/{task_id}")
async def stream_task(task_id: str, request: Request):
    """把已经发生的步骤推给页面，确认之后的新步骤也会继续推。"""
    service = get_service()
    service.snapshot(task_id)

    async def generate():
        cursor = 0
        while True:
            if await request.is_disconnected():
                break
            snapshot = service.snapshot(task_id)
            for event in snapshot["events"]:
                if event["id"] <= cursor:
                    continue
                cursor = event["id"]
                payload = json.dumps(event, ensure_ascii=False)
                yield f"event: {event['event']}\ndata: {payload}\n\n"
            if snapshot["status"] in _TERMINAL:
                yield "event: end\ndata: {}\n\n"
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(generate(), media_type="text/event-stream")
