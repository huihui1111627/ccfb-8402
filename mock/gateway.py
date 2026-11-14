"""Mock 短信网关。

对外提供：
- GET /mock/sms/outbox          人看的短信列表
- GET /mock/sms/api/latest      机器取的最近一条验证码（RPA 用）
- POST /mock/sms/api/send       测试/运维手动发短信
- POST /mock/sms/api/clear      清空
"""
from __future__ import annotations

import random
import time
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.db import get_conn

from .schema import init_mock_db

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def conn():
    c = get_conn()
    init_mock_db(c)
    get_conn().execute(
        "CREATE TABLE IF NOT EXISTS mock_sms_outbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL, body TEXT NOT NULL, "
        "window TEXT NOT NULL, created_at REAL NOT NULL, code TEXT)"
    )
    return c


def fmt(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


@router.get("/mock/sms/outbox", response_class=HTMLResponse)
def outbox(request: Request):
    c = conn()
    msgs = c.execute("SELECT * FROM mock_sms_outbox ORDER BY id DESC LIMIT 100").fetchall()
    items = [{**dict(m), "created_text": fmt(m["created_at"])} for m in msgs]
    return TEMPLATES.TemplateResponse("outbox.html", {"request": request, "items": items})


@router.get("/mock/sms/api/latest", response_class=JSONResponse)
def latest(phone: str = "", purpose: str = "", since_id: int = 0, window: str = ""):
    """返回指定手机号最近一条未消费的验证码短信。RPA 轮询这个接口。"""
    c = conn()
    q = "SELECT * FROM mock_sms WHERE consumed=0"
    params: list = []
    if phone:
        q += " AND phone=?"
        params.append(phone)
    if purpose:
        q += " AND purpose=?"
        params.append(purpose)
    if since_id:
        q += " AND id>?"
        params.append(since_id)
    q += " ORDER BY id DESC LIMIT 1"
    row = c.execute(q, params).fetchone()
    if not row:
        return JSONResponse({"found": False})
    body = c.execute(
        "SELECT body FROM mock_sms_outbox WHERE code=? AND phone=? ORDER BY id DESC LIMIT 1",
        (row["code"], row["phone"])).fetchone()
    return JSONResponse({
        "found": True,
        "id": row["id"],
        "phone": row["phone"],
        "code": row["code"],
        "purpose": row["purpose"],
        "created_at": fmt(row["created_at"]),
        "body": body["body"] if body else "",
    })


@router.get("/mock/sms/api/messages", response_class=JSONResponse)
def messages(phone: str = "", limit: int = 50):
    c = conn()
    q = "SELECT * FROM mock_sms_outbox"
    params: list = []
    if phone:
        q += " WHERE phone=?"
        params.append(phone)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(min(limit, 200))
    rows = c.execute(q, params).fetchall()
    return JSONResponse({"items": [{**dict(r), "created_at": fmt(r["created_at"])} for r in rows]})


@router.post("/mock/sms/api/send", response_class=JSONResponse)
def send(phone: str = Form(...), window: str = Form("si"), code: str = Form("")):
    c = conn()
    code = code or f"{random.randint(0, 999999):06d}"
    c.execute("INSERT INTO mock_sms(phone,code,purpose,created_at) VALUES (?,?,?,?)",
              (phone, code, "login", time.time()))
    c.execute(
        "INSERT INTO mock_sms_outbox(phone,body,window,created_at,code) VALUES (?,?,?,?,?)",
        (phone, f"【示例市政务】您的验证码为 {code}，5 分钟内有效。", window, time.time(), code),
    )
    return JSONResponse({"ok": True, "code": code})


@router.post("/mock/sms/api/clear", response_class=JSONResponse)
def clear():
    c = conn()
    c.execute("DELETE FROM mock_sms")
    c.execute("DELETE FROM mock_sms_outbox")
    return JSONResponse({"ok": True})
