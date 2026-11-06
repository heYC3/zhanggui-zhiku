"""Listing 网页接口。导入和问答还在原来的 8000、8001，这里用 8002。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.listing_agent.auth import AuthStore, SessionActor, load_accounts_from_env
from app.listing_agent.paths import MODEL_CARD_PATH, runtime_dir
from app.listing_agent.service import ListingError, ListingService


app = FastAPI(title="掌柜智库 Listing", description="商品资料检索、草稿确认和写入")
_service: ListingService | None = None
_auth: AuthStore | None = None

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
    "draft_failed",
}


def get_service() -> ListingService:
    global _service
    if _service is None:
        _service = ListingService()
    return _service


def get_auth() -> AuthStore:
    global _auth
    if _auth is None:
        store = AuthStore(runtime_dir() / "auth.sqlite3")
        load_accounts_from_env(store)
        _auth = store
    return _auth


def test_mode() -> bool:
    return os.getenv("LISTING_TEST_MODE", "").strip().lower() in {"1", "true", "yes"}


def actor_from_request(request: Request) -> SessionActor:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise ListingError("未登录", 401)
    actor = get_auth().resolve(header.split(" ", 1)[1].strip())
    if actor is None:
        raise ListingError("未登录", 401)
    return actor


@app.exception_handler(ListingError)
async def _listing_error(_request: Request, exc: ListingError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


class LoginBody(BaseModel):
    user_id: str
    password: str


class StartBody(BaseModel):
    query: str
    platform: str = "amazon"
    session_id: str | None = None
    idempotency_key: str | None = None
    rerank_mode: str = "on"
    inject_write_unknown: bool = False
    draft_mode: str | None = None


class DecisionBody(BaseModel):
    draft_version: int = 0
    reason: str = ""


class DraftBody(BaseModel):
    base_version: int
    title: str = ""
    bullets: list[str] = Field(default_factory=list)


class ReconcileBody(BaseModel):
    found: bool


def _guard_faults(rerank_mode: str, inject_write_unknown: bool) -> None:
    if not test_mode() and (inject_write_unknown or rerank_mode not in {"", "on"}):
        raise ListingError("故障注入只在测试模式开放", 403)


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


@app.post("/api/listing/sessions")
async def login(body: LoginBody):
    token = get_auth().login(body.user_id.strip(), body.password)
    if not token:
        raise ListingError("账号或密码不对", 401)
    actor = get_auth().resolve(token)
    return {
        "token": token,
        "user_id": actor.user_id,
        "role": actor.role,
        "org_id": actor.org_id,
    }


@app.post("/api/listing/tasks")
async def start_task(body: StartBody, request: Request):
    actor = actor_from_request(request)
    _guard_faults(body.rerank_mode, body.inject_write_unknown)
    return get_service().start(
        query=body.query,
        user_id=actor.user_id,
        role=actor.role,
        org_id=actor.org_id,
        platform=body.platform,
        session_id=body.session_id,
        idempotency_key=body.idempotency_key,
        rerank_mode=body.rerank_mode if test_mode() else "on",
        inject_write_unknown=bool(body.inject_write_unknown) and test_mode(),
        draft_mode=body.draft_mode,
    )


@app.get("/api/listing/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    actor = actor_from_request(request)
    return get_service().snapshot_for(task_id, actor.user_id)


@app.post("/api/listing/tasks/{task_id}/confirm")
async def confirm_task(task_id: str, body: DecisionBody, request: Request):
    actor = actor_from_request(request)
    return get_service().confirm(task_id, actor.user_id, actor.role, body.draft_version)


@app.post("/api/listing/tasks/{task_id}/reject")
async def reject_task(task_id: str, body: DecisionBody, request: Request):
    actor = actor_from_request(request)
    return get_service().reject(task_id, actor.user_id, actor.role, body.reason)


@app.post("/api/listing/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, body: DecisionBody, request: Request):
    actor = actor_from_request(request)
    return get_service().cancel(task_id, actor.user_id, actor.role)


@app.post("/api/listing/tasks/{task_id}/draft")
async def edit_draft(task_id: str, body: DraftBody, request: Request):
    actor = actor_from_request(request)
    return get_service().update_draft(
        task_id,
        actor.user_id,
        actor.role,
        body.base_version,
        body.title,
        body.bullets,
    )


@app.post("/api/listing/tasks/{task_id}/reconcile")
async def reconcile_task(task_id: str, body: ReconcileBody, request: Request):
    actor = actor_from_request(request)
    return get_service().reconcile(task_id, actor.user_id, actor.role, body.found)


async def iter_listing_events(service: ListingService, task_id: str, user_id: str, disconnected):
    """把已经落盘的步骤推出去。断开只结束这条推送，不取消任务。

    disconnected 是一个异步函数。页面用 request.is_disconnected。
    测试传入自己的函数。Starlette 的 TestClient 在任务还等确认时
    经常不送断开信号，while True 会一直睡，所以单测不走 TestClient.stream。
    """
    cursor = 0
    try:
        while True:
            if await disconnected():
                return
            snapshot = service.snapshot_for(task_id, user_id)
            for event in snapshot["events"]:
                if event["id"] <= cursor:
                    continue
                cursor = event["id"]
                payload = json.dumps(event, ensure_ascii=False)
                yield f"event: {event['event']}\ndata: {payload}\n\n"
            if snapshot["status"] in _TERMINAL:
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise


@app.get("/api/listing/stream/{task_id}")
async def stream_task(task_id: str, request: Request):
    """把已经发生的步骤推给页面。未登录 401，别人的任务 404。"""
    actor = actor_from_request(request)
    service = get_service()
    service.snapshot_for(task_id, actor.user_id)

    async def generate():
        async for chunk in iter_listing_events(
            service, task_id, actor.user_id, request.is_disconnected
        ):
            yield chunk

    return StreamingResponse(generate(), media_type="text/event-stream")
