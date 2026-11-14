"""RPA 控制台 Web 服务。

页面：总览 / 企业账号 / 花名册导入 / 待办清单 / 执行队列 / 异常处理 / 回执归档 / 审计日志。

引擎以 asyncio 任务形式跑在同一进程里，控制台与引擎共享 SQLite。
生产部署可以把 runner 拆成独立进程（多个 worker 通过 claim 互斥），接口不变。
"""
from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import quote as _urlquote
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import errors as err_mod
from . import orchestrator as orch
from . import receipts as receipt_mod
from . import writeback
from .config import ROOT, settings
from .db import get_conn, init_db, one, rows, write_tx
from .domain import ACTIONS, ERROR_CATEGORIES, STATUS_LABELS, WINDOWS
from .importer import generate_template, import_roster
from .runner import Engine

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

engine: Engine | None = None


def _status_label(st: str) -> str:
    return STATUS_LABELS.get(st, st)


def _status_class(st: str) -> str:
    return {
        "success": "ok", "duplicate": "ok",
        "needs_fix": "warn", "retry": "warn", "unknown": "warn", "queued": "muted",
        "failed": "err", "cancelled": "muted", "skipped": "muted",
        "running": "running", "pending": "muted",
    }.get(st, "muted")


def _ts(value) -> str:
    """把 ISO 时间统一显示为 YYYY-MM-DD HH:MM。"""
    if not value:
        return "—"
    s = str(value).replace("Z", "").replace("T", " ")
    return s[:16]


templates.env.filters["ts"] = _ts
templates.env.filters["status_label"] = _status_label
templates.env.filters["status_class"] = _status_class
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["ACTIONS"] = ACTIONS
templates.env.globals["WINDOWS"] = WINDOWS
templates.env.globals["ERROR_CATEGORIES"] = ERROR_CATEGORIES
templates.env.globals["status_label"] = _status_label
templates.env.globals["status_class"] = _status_class


def artifact_url(path: str) -> str:
    """把绝对文件路径转成 /artifacts 下的 URL，避免双斜杠与前导 / 问题。"""
    from .config import settings
    p = Path(path)
    for root in (settings.artifacts_dir, ROOT):
        try:
            rel = p.resolve().relative_to(root.resolve())
            return "/artifacts/" + _urlquote(str(rel))
        except ValueError:
            continue
    return "/artifacts/" + _urlquote(str(p).lstrip("/"))


def _pretty_raw(raw: str | None) -> str:
    """把隔离行里存的 JSON 转成「字段：值」的可读文本。"""
    if not raw:
        return ""
    try:
        d = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(d, dict):
        return raw
    labels = {"enterprise_code": "企业", "enterprise_name": "企业名", "employee_name": "姓名",
              "id_card": "证件号", "social_no": "社保编号", "base_before": "原基数",
              "base_after": "新基数", "phone": "手机", "remark": "备注",
              "effective_month": "生效月"}
    parts = []
    for k in ("enterprise_code", "employee_name", "id_card", "social_no",
              "base_before", "base_after", "effective_month", "phone", "remark"):
        v = d.get(k)
        if v in (None, "", []):
            continue
        parts.append(f"{labels.get(k, k)}：{v}")
    if not parts:
        # 解析期隔离的行存的是原始表头（中文键）
        parts = [f"{k}：{v}" for k, v in d.items() if v not in (None, "", [])]
    return " · ".join(parts[:8]) or raw


templates.env.filters["pretty_raw"] = _pretty_raw


def _ledger_today_count() -> int:
    """从回执台账统计今日归档数量（台账是唯一事实来源）。"""
    ledger = settings.receipts_dir / "ledger.jsonl"
    if not ledger.exists():
        return 0
    today = datetime.now().strftime("%Y-%m-%d")
    n = 0
    try:
        for line in ledger.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            if json.loads(line).get("ts", "").startswith(today):
                n += 1
    except Exception:
        return 0
    return n


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    init_db(seed=True)
    engine = Engine()
    await engine.start()
    try:
        yield
    finally:
        if engine:
            await engine.stop()


app = FastAPI(title="申报 RPA 控制台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------------------------------------------------------------- 模板上下文
def nav_counts() -> dict:
    c = get_conn()
    q = lambda sql: c.execute(sql).fetchone()[0]
    return {
        "pending": q("SELECT COUNT(*) FROM tasks WHERE status IN ('pending','queued','retry','unknown')"),
        "running": q("SELECT COUNT(*) FROM tasks WHERE status='running'"),
        "exceptions": q("SELECT COUNT(*) FROM tasks WHERE status='needs_fix'"),
    }


def ctx(request: Request, nav: str, **kw) -> dict:
    eng = engine
    mode_row = one("SELECT * FROM rate_limit_state WHERE id=1")
    base = {
        "request": request,
        "page": nav,
        "mock_url": settings.mock_base_url,
        "engine_status": "运行中" if (eng and eng._scheduler and not eng._scheduler.done()) else "已暂停",
        "nav_counts": nav_counts(),
        "mode": mode_row["mode"] if mode_row else "normal",
        "today": datetime.now().strftime("%Y-%m-%d"),
        **kw,
    }
    return base


# ---------------------------------------------------------------- 总览
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    c = get_conn()
    def count(where: str, params=()) -> int:
        return c.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}", params).fetchone()[0]

    total = count("1=1")
    success = count("status IN ('success','duplicate')")
    running = count("status='running'")
    queued = count("status IN ('queued','pending')")
    needs_fix = count("status='needs_fix'")
    failed = count("status='failed'")
    retry = count("status='retry'")
    this_month = c.execute("SELECT COUNT(*) FROM tasks WHERE substr(created_at,1,7)=?",
                           (datetime.now().strftime("%Y-%m"),)).fetchone()[0]
    receipts_total, ledger_issues = receipt_mod.verify_ledger()
    receipts_today = _ledger_today_count()

    window_stats = []
    for w, meta in WINDOWS.items():
        wt = count("window=?", (w,))
        ws = count("window=? AND status IN ('success','duplicate')", (w,))
        window_stats.append({"name": meta["name"], "total": wt, "success": ws})

    segments = [
        {"label": "已完成", "count": success, "color": "#1a7f37"},
        {"label": "执行中", "count": running, "color": "#0b5cad"},
        {"label": "排队/待执行", "count": queued - 0, "color": "#8ea3c1"},
        {"label": "重试中", "count": retry, "color": "#a15c00"},
        {"label": "待修正", "count": needs_fix, "color": "#d97706"},
        {"label": "失败", "count": failed, "color": "#b42318"},
        {"label": "其他", "count": max(0, total - success - running - queued - retry - needs_fix - failed),
         "color": "#c9d1dc"},
    ]

    attention = []
    if needs_fix:
        attention.append({"kind": "warn", "count": needs_fix, "title": "待修正任务",
                          "detail": "站点校验失败，修正花名册后可重跑", "link": "/exceptions"})
    if failed:
        attention.append({"kind": "err", "count": failed, "title": "失败任务",
                          "detail": "需要排查账号或站点问题", "link": "/exceptions"})
    unknown = count("status='unknown'")
    if unknown:
        attention.append({"kind": "warn", "count": unknown, "title": "状态待核对",
                          "detail": "提交结果未知，引擎会先到站点核对", "link": "/queue"})
    if ledger_issues:
        attention.append({"kind": "err", "count": len(ledger_issues), "title": "回执台账异常",
                          "detail": ledger_issues[0], "link": "/receipts"})
    unreg = c.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
    if unreg:
        attention.append({"kind": "warn", "count": unreg, "title": "导入隔离行",
                          "detail": "花名册中有无法识别的行", "link": "/import"})

    recent_rows = rows(
        "SELECT t.*, e.name AS enterprise_name FROM tasks t JOIN enterprises e ON e.id=t.enterprise_id "
        "WHERE t.status IN ('success','duplicate') ORDER BY t.completed_at DESC LIMIT 8")
    recent = [{**dict(r), "action_label": ACTIONS.get(r["action"], r["action"])} for r in recent_rows]

    batches = [dict(b) for b in rows("SELECT * FROM batches ORDER BY created_at DESC LIMIT 5")]

    stats = {
        "total": total, "success": success, "running": running, "queued": queued,
        "needs_fix": needs_fix, "failed": failed, "this_month": this_month,
        "success_rate": round(success / total * 100, 1) if total else 0,
        "receipts_total": receipts_total, "receipts_today": receipts_today,
    }
    return templates.TemplateResponse("dashboard.html", ctx(
        request, "dashboard", stats=stats, window_stats=window_stats, segments=segments,
        attention=attention, recent=recent, batches=batches,
        engine={"running": bool(engine and engine._scheduler and not engine._scheduler.done())}))


# ---------------------------------------------------------------- 企业账号
@app.get("/enterprises", response_class=HTMLResponse)
def enterprises_page(request: Request):
    ents = []
    for e in rows("SELECT * FROM enterprises ORDER BY code"):
        d = dict(e)
        d["task_total"] = one("SELECT COUNT(*) FROM tasks WHERE enterprise_id=? AND status IN ('success','duplicate')", (e["id"],))[0]
        d["task_pending"] = one("SELECT COUNT(*) FROM tasks WHERE enterprise_id=? AND status IN ('pending','queued','retry','unknown')", (e["id"],))[0]
        d["task_fix"] = one("SELECT COUNT(*) FROM tasks WHERE enterprise_id=? AND status IN ('needs_fix','failed')", (e["id"],))[0]
        d["monthly_used"] = one("SELECT COUNT(*) FROM tasks WHERE enterprise_id=? AND status IN ('success','duplicate') "
                                "AND substr(completed_at,1,7)=?", (e["id"], datetime.now().strftime("%Y-%m")))[0]
        # 密码脱敏展示
        d["si_password_masked"] = "•" * 8
        d["hf_password_masked"] = "•" * 8
        ents.append(d)
    return templates.TemplateResponse("enterprises.html", ctx(request, "enterprises", ents=ents))


@app.post("/enterprises")
def enterprise_save(code: str = Form(...), name: str = Form(...),
                    si_account: str = Form(...), si_password: str = Form(""),
                    hf_account: str = Form(...), hf_password: str = Form(""),
                    phone: str = Form(...), max_parallel: int = Form(1),
                    monthly_quota: int = Form(500), priority: int = Form(0),
                    active: str = Form("1")):
    code = code.strip().upper()
    with write_tx() as c:
        existing = c.execute("SELECT * FROM enterprises WHERE code=?", (code,)).fetchone()
        if existing:
            c.execute(
                "UPDATE enterprises SET name=?, si_account=?, hf_account=?, phone=?, max_parallel=?, "
                "monthly_quota=?, priority=?, active=?, "
                "si_password=CASE WHEN ?='' THEN si_password ELSE ? END, "
                "hf_password=CASE WHEN ?='' THEN hf_password ELSE ? END WHERE code=?",
                (name, si_account, hf_account, phone, max_parallel, monthly_quota, priority,
                 1 if active == "1" else 0, si_password, si_password, hf_password, hf_password, code))
        else:
            c.execute(
                "INSERT INTO enterprises(code,name,si_account,si_password,hf_account,hf_password,phone,"
                "max_parallel,monthly_quota,priority,active) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (code, name, si_account, si_password, hf_account, hf_password, phone,
                 max_parallel, monthly_quota, priority, 1 if active == "1" else 0))
    orch.audit("operator", "enterprise_saved", None, None, {"code": code})
    return RedirectResponse("/enterprises", status_code=303)


@app.post("/enterprises/{ent_id}/toggle")
def enterprise_toggle(ent_id: int):
    with write_tx() as c:
        e = c.execute("SELECT * FROM enterprises WHERE id=?", (ent_id,)).fetchone()
        if not e:
            raise HTTPException(404)
        c.execute("UPDATE enterprises SET active=? WHERE id=?", (0 if e["active"] else 1, ent_id))
    return RedirectResponse("/enterprises", status_code=303)


# ---------------------------------------------------------------- 导入
@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    batches = []
    for b in rows("SELECT * FROM batches ORDER BY created_at DESC LIMIT 20"):
        d = dict(b)
        counts = one(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN t.status IN ('success','duplicate') THEN 1 ELSE 0 END) done, "
            "SUM(CASE WHEN t.status IN ('needs_fix','failed') THEN 1 ELSE 0 END) bad "
            "FROM batch_tasks bt JOIN tasks t ON t.id=bt.task_id WHERE bt.batch_id=?",
            (b["id"],))
        d["done"] = counts["done"] or 0
        d["bad"] = counts["bad"] or 0
        d["linked"] = counts["total"] or 0
        batches.append(d)
    quar = [{**dict(q), "pretty": _pretty_raw(q["raw"])}
            for q in rows("SELECT * FROM quarantine ORDER BY id DESC LIMIT 50")]
    return templates.TemplateResponse("import.html", ctx(
        request, "import", batches=batches, quarantine=quar,
        result=request.query_params.get("result")))


@app.post("/import")
async def import_upload(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        raise HTTPException(400, "文件为空")
    try:
        res = import_roster(content, file.filename or "roster.xlsx")
    except Exception as e:  # noqa: BLE001
        return templates.TemplateResponse("import.html", ctx(
            request, "import", batches=[], quarantine=[], result=None, error=str(e)),
            status_code=400)
    return RedirectResponse(f"/batches/{res.batch_id}", status_code=303)


@app.get("/import/template")
def import_template():
    data = generate_template()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''%E8%8A%B1%E5%90%8D%E5%86%8C%E6%A8%A1%E6%9D%BF.xlsx"})


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_page(request: Request, batch_id: str):
    b = one("SELECT * FROM batches WHERE id=?", (batch_id,))
    if not b:
        raise HTTPException(404, "批次不存在")
    tasks = []
    for t in rows(
            "SELECT t.*, e.name AS enterprise_name, e.code AS enterprise_code, bt.row_no, bt.outcome "
            "FROM batch_tasks bt JOIN tasks t ON t.id=bt.task_id "
            "JOIN enterprises e ON e.id=t.enterprise_id WHERE bt.batch_id=? ORDER BY bt.row_no, t.id",
            (batch_id,)):
        tasks.append({**dict(t), "action_label": ACTIONS.get(t["action"], t["action"]),
                      "window_label": WINDOWS.get(t["window"], {}).get("short", t["window"])})
    quar = [{**dict(q), "pretty": _pretty_raw(q["raw"])}
            for q in rows("SELECT * FROM quarantine WHERE batch_id=? ORDER BY row_no", (batch_id,))]
    counts: dict[str, int] = {}
    seen_task: set[int] = set()
    for t in tasks:
        if t["id"] in seen_task:
            continue
        seen_task.add(t["id"])
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    counts["_rows"] = len(tasks)
    counts["_distinct"] = len(seen_task)
    out = writeback.OUT_DIR / batch_id / "result.xlsx"
    return templates.TemplateResponse("batch.html", ctx(
        request, "import", batch=dict(b), tasks=tasks, quarantine=quar, counts=counts,
        has_result=out.exists(), batch_id=batch_id))


@app.get("/batches/{batch_id}/result.xlsx")
def batch_result(batch_id: str):
    path = writeback.build_result_workbook(batch_id)
    if not path or not path.exists():
        raise HTTPException(404, "结果文件尚未生成")
    return FileResponse(path, filename=f"申报结果_{batch_id}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/batches/{batch_id}/original")
def batch_original(batch_id: str):
    b = one("SELECT * FROM batches WHERE id=?", (batch_id,))
    if not b:
        raise HTTPException(404)
    name = b["filename"]
    src = writeback.IN_DIR / batch_id / Path(name).name
    if not src.exists():
        raise HTTPException(404)
    return FileResponse(src, filename=name)


# ---------------------------------------------------------------- 待办清单
@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, status: str = "", window: str = "", enterprise: str = "",
               action: str = "", q: str = "", page: int = 1):
    where = ["1=1"]
    params: list = []
    if status == "active":
        where.append("t.status IN ('pending','queued','running','retry','unknown')")
    elif status:
        where.append("t.status=?")
        params.append(status)
    if window:
        where.append("t.window=?")
        params.append(window)
    if enterprise:
        where.append("t.enterprise_id=?")
        params.append(int(enterprise))
    if action:
        where.append("t.action=?")
        params.append(action)
    if q:
        where.append("(t.employee_name LIKE ? OR t.id_card LIKE ? OR t.receipt_no LIKE ?)")
        params += [f"%{q}%"] * 3
    wsql = " AND ".join(where)
    total = one(f"SELECT COUNT(*) FROM tasks t WHERE {wsql}", params)[0]
    per = 50
    offset = max(0, (page - 1) * per)
    task_rows = rows(
        f"SELECT t.*, e.name AS enterprise_name, e.code AS enterprise_code FROM tasks t "
        f"JOIN enterprises e ON e.id=t.enterprise_id WHERE {wsql} ORDER BY t.id DESC LIMIT ? OFFSET ?",
        [*params, per, offset])
    tasks = []
    for t in task_rows:
        d = {**dict(t)}
        d["action_label"] = ACTIONS.get(t["action"], t["action"])
        d["window_label"] = WINDOWS.get(t["window"], {}).get("short", t["window"])
        d["error_label"] = ERROR_CATEGORIES.get(t["error_code"], {}).get("label") if t["error_code"] else None
        tasks.append(d)
    status_tabs = [("", "全部"), ("active", "进行中"), ("success", "已完成"),
                   ("needs_fix", "待修正"), ("failed", "失败"), ("skipped", "已跳过")]
    ents = [dict(e) for e in rows("SELECT id, code, name FROM enterprises ORDER BY code")]
    return templates.TemplateResponse("tasks.html", ctx(
        request, "tasks", tasks=tasks, total=total, page=page, pages=max(1, (total + per - 1) // per),
        status=status, window=window, enterprise=enterprise, action=action, q=q,
        status_tabs=status_tabs, ents=ents))


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: int):
    t = one("SELECT t.*, e.name AS enterprise_name, e.code AS enterprise_code FROM tasks t "
            "JOIN enterprises e ON e.id=t.enterprise_id WHERE t.id=?", (task_id,))
    if not t:
        raise HTTPException(404, "任务不存在")
    task = dict(t)
    task["action_label"] = ACTIONS.get(t["action"], t["action"])
    task["window_label"] = WINDOWS.get(t["window"], {}).get("short", t["window"])
    err = err_mod.task_error(t)
    executions = []
    for x in rows("SELECT * FROM executions WHERE task_id=? ORDER BY id", (task_id,)):
        d = dict(x)
        try:
            d["log_obj"] = json.loads(x["log"] or "{}")
        except Exception:
            d["log_obj"] = {}
        d["shots"] = d["log_obj"].get("screenshots", [])
        executions.append(d)
    # 失败时的截图在 error_meta 里（executions.log 只在成功时写），两者合并去重
    try:
        err_shots = json.loads(t["error_meta"] or "{}").get("screenshots", [])
    except Exception:
        err_shots = []
    seen: set[str] = set()
    shots = []
    for s in err_shots + [p for x in executions for p in x["shots"]]:
        if s in seen:
            continue
        seen.add(s)
        shots.append({"name": Path(s).name, "url": artifact_url(s)})
    events = [dict(e) for e in rows("SELECT * FROM audit_events WHERE task_id=? ORDER BY id DESC LIMIT 30", (task_id,))]
    payload = {}
    try:
        payload = json.loads(t["payload"] or "{}")
    except Exception:
        pass
    return templates.TemplateResponse("task_detail.html", ctx(
        request, "tasks", task=task, err=err, executions=executions, shots=shots,
        events=events, payload=payload))


# ---------------------------------------------------------------- 执行队列
@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    by_status: dict[str, list] = {}
    for t in rows("SELECT t.*, e.name AS enterprise_name, e.code AS enterprise_code FROM tasks t "
                  "JOIN enterprises e ON e.id=t.enterprise_id "
                  "WHERE t.status IN ('running','queued','pending','retry','unknown') "
                  "ORDER BY t.id"):
        d = {**dict(t)}
        d["action_label"] = ACTIONS.get(t["action"], t["action"])
        d["window_label"] = WINDOWS.get(t["window"], {}).get("short", t["window"])
        by_status.setdefault(t["status"], []).append(d)
    # 每个企业的并发占用
    ent_load = rows(
        "SELECT e.code, e.name, e.max_parallel, "
        "SUM(CASE WHEN t.status='running' THEN 1 ELSE 0 END) running, "
        "SUM(CASE WHEN t.status IN ('queued','pending','retry','unknown') THEN 1 ELSE 0 END) waiting "
        "FROM enterprises e LEFT JOIN tasks t ON t.enterprise_id=e.id GROUP BY e.id ORDER BY e.code")
    eng = engine
    running_now = len(eng.running) if eng else 0
    global_c, per_ent, per_min = eng.rate_config() if eng else (0, 1, 0)
    db_counts = {r["status"]: r["n"] for r in rows(
        "SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
    return templates.TemplateResponse("queue.html", ctx(
        request, "queue", by_status=by_status, ent_load=[dict(r) for r in ent_load],
        running_now=running_now, global_c=global_c, per_min=per_min,
        limiter=eng.rate if eng else None, db_counts=db_counts,
        engine_running=bool(eng and eng._scheduler and not eng._scheduler.done()),
        stats=eng.stats if eng else {}))


@app.get("/api/queue/state")
def queue_state():
    c = get_conn()
    counts = {r["status"]: r["n"] for r in c.execute(
        "SELECT status, COUNT(*) n FROM tasks WHERE status IN ('running','queued','pending','retry','unknown') GROUP BY status")}
    return JSONResponse({
        "counts": counts,
        "running": len(engine.running) if engine else 0,
        "stats": engine.stats if engine else {},
        "engine_running": bool(engine and engine._scheduler and not engine._scheduler.done()),
        "ts": _now_text(),
    })


@app.post("/api/rate-mode")
async def set_rate_mode(mode: str = Form(None), request: Request = None):
    if request is not None and request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        mode = body.get("mode", mode)
    if mode not in ("normal", "peak", "offpeak"):
        raise HTTPException(400, "mode 必须是 normal/peak/offpeak")
    with write_tx() as c:
        c.execute("UPDATE rate_limit_state SET mode=?, updated_at=? WHERE id=1",
                  (mode, _now_text()))
    orch.audit("operator", "rate_mode_changed", None, None, {"mode": mode})
    label = {"normal": "常规", "peak": "月底高峰（限流）", "offpeak": "平峰"}[mode]
    return JSONResponse({"mode": mode, "label": label})


@app.post("/api/engine/start")
async def engine_start():
    if engine:
        await engine.start()
        orch.audit("operator", "engine_started")
    return JSONResponse({"ok": True, "message": "引擎已启动"})


@app.post("/api/engine/stop")
async def engine_stop():
    if engine:
        await engine.stop()
        orch.audit("operator", "engine_stopped")
    return JSONResponse({"ok": True, "message": "引擎已暂停（执行中的任务会跑完）"})


# ---------------------------------------------------------------- 异常处理
@app.get("/exceptions", response_class=HTMLResponse)
def exceptions_page(request: Request, code: str = ""):
    groups = err_mod.error_groups()
    where = "t.status IN ('needs_fix','failed','unknown','retry') OR t.error_code='quota_exceeded'"
    params: list = []
    if code:
        where += " AND t.error_code=?"
        params.append(code)
    task_rows = rows(
        f"SELECT t.*, e.name AS enterprise_name, e.code AS enterprise_code FROM tasks t "
        f"JOIN enterprises e ON e.id=t.enterprise_id WHERE {where} ORDER BY t.id DESC LIMIT 200", params)
    tasks = []
    for t in task_rows:
        d = {**dict(t)}
        d["action_label"] = ACTIONS.get(t["action"], t["action"])
        d["window_label"] = WINDOWS.get(t["window"], {}).get("short", t["window"])
        d["err_meta"] = ERROR_CATEGORIES.get(t["error_code"] or "", ERROR_CATEGORIES["site_error"])
        try:
            d["shots"] = [{"name": Path(p).name, "url": artifact_url(p)}
                          for p in json.loads(t["error_meta"] or "{}").get("screenshots", [])]
        except Exception:
            d["shots"] = []
        tasks.append(d)
    return templates.TemplateResponse("exceptions.html", ctx(
        request, "exceptions", groups=groups, tasks=tasks, code=code))


# ---------------------------------------------------------------- 回执归档
@app.get("/receipts", response_class=HTMLResponse)
def receipts_page(request: Request, q: str = ""):
    where = "t.status IN ('success','duplicate')"
    params: list = []
    if q:
        where += " AND (t.receipt_no LIKE ? OR t.employee_name LIKE ? OR t.fingerprint LIKE ?)"
        params = [f"%{q}%"] * 3
    task_rows = rows(
        f"SELECT t.*, e.name AS enterprise_name, e.code AS enterprise_code FROM tasks t "
        f"JOIN enterprises e ON e.id=t.enterprise_id WHERE {where} "
        f"ORDER BY t.completed_at DESC LIMIT 200", params)
    receipts = []
    for t in task_rows:
        d = {**dict(t)}
        d["action_label"] = ACTIONS.get(t["action"], t["action"])
        d["window_label"] = WINDOWS.get(t["window"], {}).get("short", t["window"])
        d["exists"] = bool(t["receipt_path"] and Path(t["receipt_path"]).exists())
        receipts.append(d)
    n, issues = receipt_mod.verify_ledger()
    ledger = []
    lp = settings.receipts_dir / "ledger.jsonl"
    if lp.exists():
        for line in lp.read_text("utf-8").splitlines()[-50:]:
            try:
                ledger.append(json.loads(line))
            except Exception:
                pass
        ledger.reverse()
    return templates.TemplateResponse("receipts.html", ctx(
        request, "receipts", receipts=receipts, q=q, ledger=ledger, ledger_count=n,
        ledger_issues=issues))


@app.get("/receipts/{task_id}")
def receipt_file(task_id: int):
    t = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t or not t["receipt_path"]:
        raise HTTPException(404, "回执不存在")
    p = Path(t["receipt_path"])
    if not p.exists():
        raise HTTPException(404, "回执文件缺失")
    return FileResponse(p, media_type="application/pdf",
                        filename=f"{t['receipt_no']}_{t['employee_name']}.pdf",
                        content_disposition_type="inline")


@app.get("/artifacts/{path:path}")
def artifact(path: str):
    path = path.lstrip("/")
    artifacts = settings.artifacts_dir.resolve()
    p = (artifacts / path).resolve()
    if not str(p).startswith(str(artifacts) + os.sep) and p != artifacts:
        raise HTTPException(404, "路径越界")
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "截图不存在")
    return FileResponse(p)


# ---------------------------------------------------------------- 审计
@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, page: int = 1):
    per = 60
    total = one("SELECT COUNT(*) FROM audit_events")[0]
    events = [dict(e) for e in rows(
        "SELECT a.*, e.name AS enterprise_name FROM audit_events a "
        "LEFT JOIN enterprises e ON e.id=a.enterprise_id ORDER BY a.id DESC LIMIT ? OFFSET ?",
        (per, (page - 1) * per))]
    return templates.TemplateResponse("audit.html", ctx(
        request, "audit", events=events, page=page, pages=max(1, (total + per - 1) // per), total=total))


# ---------------------------------------------------------------- API：任务操作
def _get_task(task_id: int):
    t = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


async def _json_body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


@app.post("/api/tasks/{task_id}/requeue")
async def api_requeue(task_id: int, request: Request):
    body = await _json_body(request)
    try:
        err_mod.requeue(task_id, reset_attempts=body.get("reset_attempts", True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if engine and body.get("run_now"):
        asyncio.create_task(engine.run_one_now(task_id))
    return JSONResponse({"ok": True, "task_id": task_id})


@app.post("/api/tasks/{task_id}/run")
async def api_run(task_id: int):
    t = _get_task(task_id)
    if t["status"] == "success":
        raise HTTPException(400, "已成功的任务不能重跑（幂等保护）")
    err_mod.requeue(task_id)
    if engine:
        asyncio.create_task(engine.run_one_now(task_id))
    return JSONResponse({"ok": True})


@app.post("/api/tasks/{task_id}/skip")
async def api_skip(task_id: int, request: Request):
    body = await _json_body(request)
    try:
        err_mod.skip(task_id, reason=body.get("reason", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True})


@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel(task_id: int, request: Request):
    body = await _json_body(request)
    try:
        err_mod.cancel(task_id, reason=body.get("reason", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True})


@app.post("/api/tasks/{task_id}/fix")
async def api_fix(task_id: int, request: Request):
    body = await _json_body(request)
    changes = body.get("changes") or {}
    try:
        res = err_mod.apply_fix(task_id, changes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, **res})


@app.post("/api/batches/{batch_id}/run")
async def api_batch_run(batch_id: str):
    ids = [r["task_id"] for r in rows(
        "SELECT DISTINCT t.id AS task_id FROM batch_tasks bt JOIN tasks t ON t.id=bt.task_id "
        "WHERE bt.batch_id=? AND t.status IN ('pending','queued','retry','needs_fix')",
        (batch_id,))]
    for tid in ids:
        try:
            err_mod.requeue(tid)
        except ValueError:
            pass
    orch.audit("operator", "batch_queued", None, None, {"batch_id": batch_id, "count": len(ids)})
    return JSONResponse({"ok": True, "queued": len(ids)})


@app.get("/api/tasks/{task_id}/state")
def api_task_state(task_id: int):
    t = _get_task(task_id)
    return JSONResponse({"status": t["status"], "attempts": t["attempts"],
                         "error_code": t["error_code"], "receipt_no": t["receipt_no"]})


@app.get("/api/healthz")
def healthz():
    return {"ok": True, "engine": bool(engine and engine._scheduler and not engine._scheduler.done())}
