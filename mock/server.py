"""Mock 服务入口：把站点/网关/运维台挂到同一进程。

启动：uvicorn mock.server:app --port 8100
控制台通过 MOCK_BASE_URL 引用它（默认 http://127.0.0.1:8100）。
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from app.db import get_conn, init_db

from . import admin, gateway, site
from .schema import init_mock_db

app = FastAPI(title="Mock 社保公积金站点", docs_url=None, redoc_url=None)
app.include_router(site.router)
app.include_router(gateway.router)
app.include_router(admin.router)


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    resp = await call_next(request)
    try:
        p = request.url.path
        if ("/declare" in p or "/login" in p) and p.startswith("/mock/"):
            c = get_conn()
            c.execute("INSERT INTO mock_request_log(ts,path,status) VALUES (?,?,?)",
                      (time.time(), p, resp.status_code))
    except Exception:
        pass
    return resp


@app.on_event("startup")
def _startup() -> None:
    init_db(seed=True)
    init_mock_db(get_conn())
    gateway.conn()


@app.get("/")
def index():
    return RedirectResponse("/mock/admin")


@app.get("/healthz")
def healthz():
    return {"ok": True}
