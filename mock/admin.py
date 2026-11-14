"""Mock 运维台：注入故障、看提交流水、调限流/会话时长。

仅测试与演示用。真实环境不会暴露这类开关。
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_conn

from .schema import DEFAULT_SETTINGS, init_mock_db, set_setting

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def conn():
    c = get_conn()
    init_mock_db(c)
    return c


def fmt(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


@router.get("/mock/admin", response_class=HTMLResponse)
def admin(request: Request):
    c = conn()
    settings = {r["key"]: r["value"] for r in c.execute("SELECT * FROM mock_settings")}
    subs = c.execute("SELECT * FROM mock_submissions ORDER BY id DESC LIMIT 50").fetchall()
    sessions = c.execute("SELECT * FROM mock_sessions ORDER BY last_seen DESC LIMIT 20").fetchall()
    subs_v = [{**dict(s), "created_text": fmt(s["created_at"])} for s in subs]
    sess_v = [{**dict(s), "seen_text": fmt(s["last_seen"])} for s in sessions]
    logs = c.execute("SELECT * FROM mock_request_log ORDER BY id DESC LIMIT 30").fetchall()
    log_v = [{**dict(r), "ts_text": fmt(r["ts"])} for r in logs]
    return TEMPLATES.TemplateResponse("admin.html", {
        "request": request, "settings": settings, "subs": subs_v, "sessions": sess_v, "logs": log_v,
        "defaults": DEFAULT_SETTINGS,
    })


@router.post("/mock/admin/settings")
def update_settings(request: Request, session_ttl_seconds: str = Form(""),
                    submit_latency_ms: str = Form(""), fail_next_submit: str = Form(""),
                    drop_next_submit_response: str = Form(""), rate_limit_per_min: str = Form(""),
                    maintenance: str = Form("0"), clear_sessions: str = Form(""),
                    clear_submissions: str = Form("")):
    c = conn()
    for k, v in [("session_ttl_seconds", session_ttl_seconds), ("submit_latency_ms", submit_latency_ms),
                 ("fail_next_submit", fail_next_submit),
                 ("drop_next_submit_response", drop_next_submit_response),
                 ("rate_limit_per_min", rate_limit_per_min), ("maintenance", maintenance)]:
        if v not in (None, ""):
            set_setting(c, k, v)
    if clear_sessions:
        c.execute("DELETE FROM mock_sessions")
        c.execute("DELETE FROM mock_drafts")
    if clear_submissions:
        c.execute("DELETE FROM mock_submissions")
        c.execute("UPDATE mock_employees SET status='inactive', base=NULL WHERE id_card IN "
                  "(SELECT id_card FROM mock_employees m WHERE m.status='active' AND m.social_no IS NULL)")
    return RedirectResponse("/mock/admin", status_code=303)


@router.get("/mock/admin/api/submissions", response_class=JSONResponse)
def submissions_api():
    c = conn()
    rows = c.execute("SELECT * FROM mock_submissions ORDER BY id DESC").fetchall()
    return JSONResponse({"items": [{**dict(r), "created_text": fmt(r["created_at"])} for r in rows]})
