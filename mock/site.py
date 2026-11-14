"""Mock 社保/公积金申报站点。

刻意实现真实政务站点会遇到的那些坑，好让 RPA 引擎有真东西可打：
- 账号密码 → 短信验证码两步登录；短信验证码只能从网关取，一次性
- 会话 10 分钟过期，过期后任何操作跳回登录页
- 申报表单分 3 步，步骤之间服务端保存草稿
- 证件号/姓名/基数/在册状态都有服务端校验，错误文案不统一
- 提交有唯一约束（业务指纹），重复提交返回「已受理」
- 可注入：提交 500、响应丢失（已落库但超时）、限流、维护模式
"""
from __future__ import annotations

import json
import random
import sqlite3
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_conn
from app.domain import ACTIONS, fingerprint
from app.seed import BASE_MAX, BASE_MIN

from .schema import get_setting, init_mock_db, set_setting

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SITE_NAME = {"si": "示例市社会保险网上申报系统", "hf": "示例市住房公积金网上业务系统"}

router = APIRouter()


def conn() -> sqlite3.Connection:
    c = get_conn()
    init_mock_db(c)
    init_outbox(c)
    return c



def now() -> float:
    return time.time()


def fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def ttl_seconds(c) -> int:
    return int(get_setting(c, "session_ttl_seconds") or 600)


def get_session(request: Request, window: str) -> dict | None:
    c = conn()
    sid = request.cookies.get(f"mock_{window}_sid")
    if not sid:
        return None
    row = c.execute("SELECT * FROM mock_sessions WHERE sid=? AND window=?", (sid, window)).fetchone()
    if not row:
        return None
    if now() - row["last_seen"] > ttl_seconds(c):
        c.execute("DELETE FROM mock_sessions WHERE sid=?", (sid,))
        return None
    c.execute("UPDATE mock_sessions SET last_seen=? WHERE sid=?", (now(), sid))
    ent = c.execute("SELECT * FROM mock_enterprises WHERE code=?", (row["enterprise_code"],)).fetchone()
    return {
        "sid": sid,
        "window": window,
        "enterprise_code": row["enterprise_code"],
        "enterprise": ent,
        "ttl": max(0, int(ttl_seconds(c) - (now() - row["last_seen"]) + (now() - row["last_seen"]))),
    }


def real_ttl(c, sid: str) -> int:
    row = c.execute("SELECT last_seen FROM mock_sessions WHERE sid=?", (sid,)).fetchone()
    if not row:
        return 0
    return max(0, int(ttl_seconds(c) - (now() - row["last_seen"])))


def ctx(request: Request, window: str, session: dict | None, **kw):
    c = conn()
    base = f"/mock/{window}"
    return {
        "request": request, "base": base, "session": session,
        "enterprise": session["enterprise"] if session else None,
        "site_name": SITE_NAME[window], "window": window,
        "action_labels": ACTIONS,
        "ttl": real_ttl(c, session["sid"]) if session else 0,
        "phone_masked": "*******" + session["enterprise"]["phone"][-4:] if session else "",
        **kw,
    }


def redirect_login(window: str, expired: bool = False) -> RedirectResponse:
    url = f"/mock/{window}/login"
    if expired:
        url += "?expired=1"
    return RedirectResponse(url, status_code=303)


def issue_session(window: str, code: str) -> str:
    c = conn()
    sid = uuid.uuid4().hex
    c.execute("INSERT INTO mock_sessions(sid,window,enterprise_code,created_at,last_seen) VALUES (?,?,?,?,?)",
              (sid, window, code, now(), now()))
    return sid


def send_sms(c, phone: str, purpose: str, window: str) -> str:
    code = f"{random.SystemRandom().randint(0, 999999):06d}"
    c.execute("INSERT INTO mock_sms(phone,code,purpose,created_at) VALUES (?,?,?,?)",
              (phone, code, purpose, now()))
    # 同时作为一条「短信」出现在网关页面
    c.execute(
        "INSERT INTO mock_sms_outbox(phone, body, window, created_at, code) VALUES (?,?,?,?,?)",
        (phone, f"【示例市政务】您的验证码为 {code}，5 分钟内有效。如非本人操作请忽略。",
         window, now(), code),
    )
    return code


def init_outbox(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mock_sms_outbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL, body TEXT NOT NULL, "
        "window TEXT NOT NULL, created_at REAL NOT NULL, code TEXT)"
    )




# ---------------------------------------------------------------- 登录
@router.get("/mock/{window}/login", response_class=HTMLResponse)
def login_page(request: Request, window: str, expired: int = 0):
    if window not in SITE_NAME:
        return PlainTextResponse("unknown window", status_code=404)
    notice = "登录会话已过期，请重新登录。" if expired else None
    return TEMPLATES.TemplateResponse(
        "login.html", ctx(request, window, None, step="credential", notice=notice, error=None)
    )


@router.post("/mock/{window}/login")
def login_submit(request: Request, window: str,
                 account: str = Form(""), password: str = Form(""),
                 code: str = Form(""), challenge: str = Form(""),
                 step: str = Form("credential"), resend: str = Form("")):
    c = conn()
    acc_col = f"{window}_account"
    pwd_col = f"{window}_password"
    ent = c.execute(f"SELECT * FROM mock_enterprises WHERE {acc_col}=?", (account.strip(),)).fetchone()

    def fail(msg: str, *, st: str = step, chal: str = challenge, acc: str = account, pwd: str = password):
        return TEMPLATES.TemplateResponse(
            "login.html",
            ctx(request, window, None, step=st, error=msg, account=acc, password=pwd,
                challenge=chal, phone_masked=""),
            status_code=400,
        )

    if not ent or ent[pwd_col] != password:
        return fail("单位账号或密码不正确。")

    if step != "sms" or not challenge:
        ch = uuid.uuid4().hex
        send_sms(c, ent["phone"], "login", window)
        return TEMPLATES.TemplateResponse(
            "login.html",
            ctx(request, window, None, step="sms", challenge=ch, account=account,
                password=password, phone_masked="*******" + ent["phone"][-4:], error=None, site_name=SITE_NAME[window]),
        )

    if resend:
        send_sms(c, ent["phone"], "login", window)
        return TEMPLATES.TemplateResponse(
            "login.html",
            ctx(request, window, None, step="sms", challenge=challenge, account=account,
                password=password, phone_masked="*******" + ent["phone"][-4:],
                notice="新的验证码已发送。"),
        )

    # 校验验证码：取该手机号最近一条未消费、5 分钟内的短信
    row = c.execute(
        "SELECT id,code FROM mock_sms WHERE phone=? AND consumed=0 AND created_at>? "
        "ORDER BY id DESC LIMIT 1", (ent["phone"], now() - 300)).fetchone()
    if not row or row["code"] != code.strip():
        return fail("短信验证码不正确或已失效。")
    c.execute("UPDATE mock_sms SET consumed=1 WHERE id=?", (row["id"],))

    sid = issue_session(window, ent["code"])
    # 记录登录短信到 outbox 内容已在上方
    resp = RedirectResponse(f"/mock/{window}/", status_code=303)
    resp.set_cookie(f"mock_{window}_sid", sid, httponly=True, samesite="lax")
    return resp


@router.post("/mock/{window}/logout")
def logout(request: Request, window: str):
    sid = request.cookies.get(f"mock_{window}_sid")
    if sid:
        conn().execute("DELETE FROM mock_sessions WHERE sid=?", (sid,))
    resp = RedirectResponse(f"/mock/{window}/login", status_code=303)
    resp.delete_cookie(f"mock_{window}_sid")
    return resp


# ---------------------------------------------------------------- 首页
@router.get("/mock/{window}/", response_class=HTMLResponse)
def home(request: Request, window: str):
    s = get_session(request, window)
    if not s:
        return redirect_login(window, expired=bool(request.cookies.get(f"mock_{window}_sid")))
    c = conn()
    ent = s["enterprise"]
    active = c.execute("SELECT COUNT(*) FROM mock_employees WHERE enterprise_code=? AND status='active'",
                       (ent["code"],)).fetchone()[0]
    month = time.strftime("%Y-%m")
    monthly = c.execute("SELECT COUNT(*) FROM mock_submissions WHERE enterprise_code=? AND effective_month=?",
                        (ent["code"], month)).fetchone()[0]
    recent = c.execute(
        "SELECT * FROM mock_submissions WHERE enterprise_code=? ORDER BY id DESC LIMIT 5",
        (ent["code"],)).fetchall()
    recent_v = [{**dict(r), "created_text": fmt_ts(r["created_at"])} for r in recent]
    rate_notice = None
    if get_setting(c, "maintenance") == "1":
        rate_notice = "系统维护中，申报业务暂停受理，请稍后再试。"
    elif int(get_setting(c, "rate_limit_per_min") or 0) > 0:
        rate_notice = "当前为业务高峰期，申报请求将进行限流，请耐心等待。"
    return TEMPLATES.TemplateResponse(
        "home.html", ctx(request, window, s, enterprise=ent, counts={"active": active, "monthly": monthly},
                         recent=recent_v, rate_notice=rate_notice)
    )


# ---------------------------------------------------------------- 申报
def get_draft(c, draft_id: str | None, window: str) -> sqlite3.Row | None:
    if not draft_id:
        return None
    return c.execute("SELECT * FROM mock_drafts WHERE draft_id=? AND window=?", (draft_id, window)).fetchone()


@router.get("/mock/{window}/declare", response_class=HTMLResponse)
def declare_get(request: Request, window: str, action: str = "add", step: int = 1,
                draft_id: str = ""):
    s = get_session(request, window)
    if not s:
        return redirect_login(window, expired=True)
    if action not in ACTIONS:
        action = "add"
    c = conn()
    if get_setting(c, "maintenance") == "1":
        return TEMPLATES.TemplateResponse("form.html", ctx(
            request, window, s, action=action, step=0, total_steps=3, step_title="系统维护",
            v={}, draft_id="", error="系统维护中，暂不受理申报。", error_code="site_error"))
    draft = get_draft(c, draft_id, window)
    if step == 3 and draft:
        return TEMPLATES.TemplateResponse("form.html", ctx(
            request, window, s, action=draft["action"], step=3, total_steps=3,
            step_title="提交受理", v=json.loads(draft["values_json"]), draft_id=draft_id,
            error=None, error_code=None))
    return TEMPLATES.TemplateResponse("form.html", ctx(
        request, window, s, action=action, step=1, total_steps=3, step_title="填写信息",
        v={}, draft_id="", error=None, error_code=None))


def validate_server(c, window: str, ent: sqlite3.Row, action: str, v: dict) -> tuple[str | None, str | None]:
    """返回 (错误分类, 错误文案)。文案故意不统一，RPA 需要归类。"""
    name = (v.get("employee_name") or "").strip()
    idc = (v.get("id_card") or "").strip().upper()
    month = (v.get("effective_month") or "").strip()
    base_after = v.get("base_after")
    social_no = (v.get("social_no") or "").strip()

    import re
    if not re.match(r"^\d{4}-\d{2}$", month) or not (1 <= int(month[5:]) <= 12):
        return "invalid_field", "【E102】生效月份格式不正确，应形如 2026-11。"

    if not re.match(r"^[0-9X]{15,18}$", idc):
        return "id_card_invalid", "【E201】证件号码格式错误：仅支持 15 位或 18 位身份证号。"

    emp = c.execute("SELECT * FROM mock_employees WHERE enterprise_code=? AND id_card=?",
                    (ent["code"], idc)).fetchone()

    # 姓名一致才继续；姓名不一致时不暴露站点里的真实姓名（真实站点也不会返回）
    if emp and emp["name"] != name:
        return "name_mismatch", "【E205】姓名与证件号码不匹配，请核对后重新填写。"

    if action == "add":
        if not base_after:
            return "invalid_field", "【E103】请填写申报缴费基数。"
        try:
            b = float(base_after)
        except ValueError:
            return "invalid_field", "【E104】缴费基数必须为数字。"
        if not (BASE_MIN <= b <= BASE_MAX):
            return ("base_out_of_range",
                    f"【E301】缴费基数超出规定范围（{int(BASE_MIN)}—{int(BASE_MAX)} 元），请调整后重新申报。")
        if emp and emp["status"] == "active":
            return "already_exists", "【E401】该人员在当前单位已处于参保状态，不能重复增员。"
        if not emp:
            # 站点对新人也做姓名+证件一致性校验：mock 说「库里没有该证件」
            return "not_found", "【E206】人员信息库中未查询到该证件号码，请先办理人员信息采集。"
    elif action == "remove":
        if not emp or emp["status"] != "active":
            return "not_found", "【E402】该人员不在当前单位参保名册中，无法办理减员。"
        if social_no and emp["social_no"] and social_no != emp["social_no"]:
            return "account_mismatch", "【E203】社保编号与证件号码不匹配。"
    elif action == "adjust":
        if not base_after:
            return "invalid_field", "【E103】请填写调整后的缴费基数。"
        try:
            b = float(base_after)
        except ValueError:
            return "invalid_field", "【E104】缴费基数必须为数字。"
        if not (BASE_MIN <= b <= BASE_MAX):
            return ("base_out_of_range",
                    f"【E301】调整后基数超出规定范围（{int(BASE_MIN)}—{int(BASE_MAX)} 元）。")
        if not emp or emp["status"] != "active":
            return "not_found", "【E403】该人员不在参保名册中，不能办理基数调整。"
        if emp["base"] is not None and abs(float(emp["base"]) - b) < 0.01:
            return "invalid_field", "【E105】调整后基数与原基数相同，无需调整。"
    return None, None


@router.post("/mock/{window}/declare", response_class=HTMLResponse)
def declare_post(request: Request, window: str, action: str = Form("add"), step: int = Form(1),
                 draft_id: str = Form(""), employee_name: str = Form(""), id_card: str = Form(""),
                 social_no: str = Form(""), base_after: str = Form(""), effective_month: str = Form(""),
                 contact_phone: str = Form(""), back: str = Form(""), confirm: str = Form("")):
    s = get_session(request, window)
    if not s:
        return redirect_login(window, expired=True)
    c = conn()
    ent = s["enterprise"]
    draft = get_draft(c, draft_id, window)

    v = {"employee_name": employee_name, "id_card": id_card, "social_no": social_no,
         "base_after": base_after, "effective_month": effective_month, "contact_phone": contact_phone}
    if draft and step >= 2 and not employee_name:
        # 从草稿恢复（例如会话续跑只带回来了 draft_id）
        v = json.loads(draft["values_json"])
        action = draft["action"]

    def render(st: int, err: str | None, code: str | None, vv: dict, did: str):
        titles = {1: "填写信息", 2: "确认信息", 3: "提交受理", 0: "系统维护"}
        return TEMPLATES.TemplateResponse("form.html", ctx(
            request, window, s, action=action, step=st, total_steps=3, step_title=titles.get(st, ""),
            v=vv, draft_id=did, error=err, error_code=code))

    if get_setting(c, "maintenance") == "1":
        return render(0, "系统维护中，暂不受理申报。", "site_error", v, draft_id)

    # 测试注入：模拟填表到一半会话被站点踢掉
    expire_on_step = int(get_setting(c, "expire_on_step") or 0)
    if expire_on_step and step == expire_on_step and not back:
        c.execute("DELETE FROM mock_sessions WHERE sid=?", (s["sid"],))
        set_setting(c, "expire_on_step", "0")
        return RedirectResponse(f"/mock/{window}/login?expired=1", status_code=303)

    # 上一步
    if back:
        if step == 2:
            return render(1, None, None, v, draft_id)
        if step == 3:
            return render(2, None, None, v, draft_id)
        return render(1, None, None, v, draft_id)

    if step == 1 or (draft and not employee_name and step == 1):
        err, msg = validate_server(c, window, ent, action, v)
        if err:
            return render(1, msg, err, v, draft_id)
        did = uuid.uuid4().hex
        c.execute("INSERT INTO mock_drafts(draft_id,window,enterprise_code,action,values_json,created_at) "
                  "VALUES (?,?,?,?,?,?)",
                  (did, window, ent["code"], action, json.dumps(v, ensure_ascii=False), now()))
        return render(2, None, None, v, did)

    if step == 2:
        err, msg = validate_server(c, window, ent, action, v)
        if err:
            return render(1, msg, err, v, draft_id)
        if draft_id:
            c.execute("UPDATE mock_drafts SET values_json=? WHERE draft_id=?",
                      (json.dumps(v, ensure_ascii=False), draft_id))
        return RedirectResponse(f"/mock/{window}/declare?action={action}&step=3&draft_id={draft_id}",
                                status_code=303)

    if step == 3:
        err, msg = validate_server(c, window, ent, action, v)
        if err:
            return render(1, msg, err, v, draft_id)

        # ---- 提交
        fp = fingerprint(ent["code"], window, (v["id_card"] or "").upper(), action,
                         (v["effective_month"] or "").strip())

        # 限流（提交动作才计入，读取/填表不计）
        rpm = int(get_setting(c, "rate_limit_per_min") or 0)
        if rpm > 0:
            window_s = float(get_setting(c, "rate_limit_window_seconds") or 60)
            until = now() - window_s
            # 只统计窗口内已受理的提交笔数；填表/查询不占配额（真实站点也按受理笔数限流）
            hits = c.execute("SELECT COUNT(*) FROM mock_submissions WHERE created_at>?",
                             (until,)).fetchone()[0]
            if hits >= rpm:
                return TEMPLATES.TemplateResponse("form.html", ctx(
                    request, window, s, action=action, step=0, total_steps=3, step_title="系统限流",
                    v=v, draft_id=draft_id,
                    error="【E500】当前访问人数过多，请稍后再试。", error_code="rate_limited"), status_code=429)

        existing = c.execute("SELECT * FROM mock_submissions WHERE window=? AND enterprise_code=? AND fingerprint=?",
                             (window, ent["code"], fp)).fetchone()
        if existing:
            return TEMPLATES.TemplateResponse(
                "receipt.html", ctx(request, window, s, sub={**dict(existing),
                                                              "created_text": fmt_ts(existing["created_at"])}))

        # 注入故障：提交前 500（未落库）
        if int(get_setting(c, "fail_next_submit") or 0) > 0:
            set_setting(c, "fail_next_submit", str(int(get_setting(c, "fail_next_submit")) - 1))
            return TEMPLATES.TemplateResponse("form.html", ctx(
                request, window, s, action=action, step=3, total_steps=3, step_title="提交受理",
                v=v, draft_id=draft_id,
                error="【E900】系统繁忙，请稍后重试（本次提交未受理）。", error_code="site_error"), status_code=500)

        # ---- 落库
        receipt_no = f"{window.upper()}{time.strftime('%Y%m')}{random.randint(100000, 999999)}"
        emp = c.execute("SELECT * FROM mock_employees WHERE enterprise_code=? AND id_card=?",
                        (ent["code"], (v["id_card"] or "").upper())).fetchone()
        base_before = emp["base"] if emp else None
        base_after_val = float(v["base_after"]) if v.get("base_after") else None
        try:
            c.execute(
                "INSERT INTO mock_submissions(window,enterprise_code,fingerprint,action,employee_name,id_card,"
                "social_no,base_before,base_after,effective_month,receipt_no,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (window, ent["code"], fp, action, v["employee_name"], (v["id_card"] or "").upper(),
                 v.get("social_no") or (emp["social_no"] if emp else None), base_before, base_after_val,
                 (v["effective_month"] or "").strip(), receipt_no, now()))
        except sqlite3.IntegrityError:
            existing = c.execute("SELECT * FROM mock_submissions WHERE window=? AND enterprise_code=? AND fingerprint=?",
                                 (window, ent["code"], fp)).fetchone()
            return TEMPLATES.TemplateResponse(
                "receipt.html", ctx(request, window, s, sub={**dict(existing),
                                                              "created_text": fmt_ts(existing["created_at"])}))

        # 更新 mock 名册状态
        if action == "add" and emp:
            c.execute("UPDATE mock_employees SET status='active', base=? WHERE id=?", (base_after_val, emp["id"]))
        elif action == "remove" and emp:
            c.execute("UPDATE mock_employees SET status='inactive' WHERE id=?", (emp["id"],))
        elif action == "adjust" and emp:
            c.execute("UPDATE mock_employees SET base=? WHERE id=?", (base_after_val, emp["id"]))
        if draft_id:
            c.execute("DELETE FROM mock_drafts WHERE draft_id=?", (draft_id,))

        sub = c.execute("SELECT * FROM mock_submissions WHERE receipt_no=?", (receipt_no,)).fetchone()

        # 注入故障：已落库，但响应丢失
        if int(get_setting(c, "drop_next_submit_response") or 0) > 0:
            set_setting(c, "drop_next_submit_response", str(int(get_setting(c, "drop_next_submit_response")) - 1))
            return PlainTextResponse("upstream timeout", status_code=504)

        return TEMPLATES.TemplateResponse(
            "receipt.html", ctx(request, window, s, sub={**dict(sub), "created_text": fmt_ts(sub["created_at"])}))

    return render(1, "未知步骤", "invalid_field", v, draft_id)


@router.get("/mock/{window}/receipt/{receipt_no}", response_class=HTMLResponse)
def receipt_page(request: Request, window: str, receipt_no: str):
    s = get_session(request, window)
    if not s:
        return redirect_login(window, expired=True)
    c = conn()
    sub = c.execute("SELECT * FROM mock_submissions WHERE receipt_no=? AND window=?",
                    (receipt_no, window)).fetchone()
    if not sub:
        return TEMPLATES.TemplateResponse("form.html", ctx(
            request, window, s, action="add", step=0, total_steps=3, step_title="查询失败",
            v={}, draft_id="", error="未查询到该受理编号。", error_code="not_found"), status_code=404)
    return TEMPLATES.TemplateResponse(
        "receipt.html", ctx(request, window, s, sub={**dict(sub), "created_text": fmt_ts(sub["created_at"])}))


@router.get("/mock/{window}/query", response_class=JSONResponse)
def query_submission(request: Request, window: str, fingerprint: str = "", receipt_no: str = ""):
    """幂等核对接口：RPA 在提交结果未知时用它确认是否已受理。"""
    s = get_session(request, window)
    if not s:
        return JSONResponse({"error": "session_expired"}, status_code=401)
    c = conn()
    q = "SELECT * FROM mock_submissions WHERE window=? AND enterprise_code=?"
    params: list = [window, s["enterprise_code"]]
    if fingerprint:
        q += " AND fingerprint=?"
        params.append(fingerprint)
    elif receipt_no:
        q += " AND receipt_no=?"
        params.append(receipt_no)
    else:
        return JSONResponse({"error": "need fingerprint or receipt_no"}, status_code=400)
    row = c.execute(q, params).fetchone()
    if not row:
        return JSONResponse({"found": False})
    return JSONResponse({"found": True, "receipt_no": row["receipt_no"], "action": row["action"],
                         "employee_name": row["employee_name"], "effective_month": row["effective_month"],
                         "created_at": fmt_ts(row["created_at"])})
