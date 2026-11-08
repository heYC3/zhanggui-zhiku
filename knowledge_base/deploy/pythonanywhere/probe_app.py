"""PythonAnywhere 最小 ASGI 探针；不加载业务凭据，不提供商品写入接口。"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
import uuid

from fastapi import FastAPI


def create_app(runtime: Path | None = None) -> FastAPI:
    directory = runtime or Path(os.getenv("PA_PROBE_RUNTIME", str(Path.home() / ".local/share/zhanggui/asgi-probe")))

    @asynccontextmanager
    async def lifespan(app):
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(directory / "probe.sqlite3")
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, marker TEXT, boots INTEGER)")
                connection.execute("INSERT OR IGNORE INTO probe VALUES (1, ?, 0)", (uuid.uuid4().hex,))
                connection.execute("UPDATE probe SET boots = boots + 1 WHERE id = 1")
            marker, boots = connection.execute("SELECT marker, boots FROM probe WHERE id = 1").fetchone()
            app.state.proof = {"ok": True, "service": "zhanggui-asgi-probe", "protocol": "asgi",
                               "marker": marker, "boot_count": boots}
            yield
        finally:
            connection.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    @app.get("/health")
    async def health():
        return app.state.proof

    return app


app = create_app()
