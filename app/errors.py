"""错误归类、处置建议与「改完重跑」。

归类的目的不是展示好看的文案，而是决定**下一步能不能自动做**：
- auto_retry=True：登录过期/限流/站点 5xx/结果未知 → 自动退避重排
- retryable_after_fix=True：数据问题 → 停在「待修正」，等花名册改完后人工重跑
- 其余（账号密码错等）→ 失败，需要运维介入
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .db import one, rows, write_tx
from .domain import ERROR_CATEGORIES, fingerprint
from . import orchestrator as orch


def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def classify(exc: Exception) -> tuple[str, str, dict]:
    """把异常映射为 (错误分类, 文案, 附加信息)。"""
    from .flow import SessionExpired, SiteUnavailable, SiteValidationError, SubmitOutcomeUnknown

    if isinstance(exc, SessionExpired):
        return "session_expired", "登录会话过期", {}
    if isinstance(exc, SubmitOutcomeUnknown):
        return "submit_unknown", str(exc), {}
    if isinstance(exc, SiteUnavailable):
        return "site_error", str(exc), {"status": getattr(exc, "status", None)}
    if isinstance(exc, SiteValidationError):
        code, msg = orch.classify_error(exc.message)
        if exc.code in ERROR_CATEGORIES and exc.code not in ("invalid_field",):
            # 页面自带分类时优先（例如限流/维护）
            if exc.code in ("rate_limited", "site_error", "session_expired"):
                code = exc.code
        return code, exc.message, {"site_code": exc.code}
    return "site_error", f"{type(exc).__name__}: {exc}", {}


def disposition(code: str, attempts: int, max_attempts: int) -> tuple[str, str | None]:
    """返回 (目标状态, next_retry_at)。"""
    meta = ERROR_CATEGORIES.get(code, ERROR_CATEGORIES["site_error"])
    if meta.get("auto_retry"):
        if attempts >= max_attempts:
            return "needs_fix", None
        from .config import settings
        backoff = min(settings.backoff_base_s * (2 ** max(0, attempts - 1)),
                      settings.backoff_max_s)
        if code == "rate_limited":
            backoff = max(backoff, settings.rate_limit_floor_s)
        return "retry", _iso(_now() + timedelta(seconds=backoff))
    if meta.get("retryable_after_fix"):
        return "needs_fix", None
    return "failed", None


def public(code: str) -> dict:
    meta = ERROR_CATEGORIES.get(code, ERROR_CATEGORIES["site_error"])
    return {"code": code, **meta}


def task_error(task_row) -> dict | None:
    if not task_row["error_code"]:
        return None
    return public(task_row["error_code"])


# ---------------------------------------------------------------- 人工修正
FIELD_TO_COLUMN = {
    "employee_name": "employee_name",
    "id_card": "id_card",
    "social_no": "social_no",
    "base_after": "base_after",
    "base_before": "base_before",
    "effective_month": "effective_month",
}


def apply_fix(task_id: int, changes: dict, actor: str = "operator") -> dict:
    """人工修改任务字段后重新排队。

    **修改不改变业务指纹**（企业/险种/证件号/变更类型/生效月份），因此重跑时
    仍然走同一 claim、同一站点唯一约束。如果修改本身动了指纹要素（比如换了
    证件号），调用方必须显式说明，系统会按新指纹另建任务并把原任务标为已取消。
    """
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] == "success":
        raise ValueError("已成功的任务不允许修改重跑；如需更正请走站点更正流程并新建任务")

    ent = one("SELECT * FROM enterprises WHERE id=?", (task["enterprise_id"],))
    allowed = {k: v for k, v in changes.items() if k in FIELD_TO_COLUMN and v not in (None, "")}
    if not allowed:
        raise ValueError("没有可修改的字段")

    id_card = allowed.get("id_card", task["id_card"])
    month = allowed.get("effective_month", task["effective_month"])
    new_fp = fingerprint(ent["code"], task["window"], id_card, task["action"], month)
    fp_changed = new_fp != task["fingerprint"]

    dup = one("SELECT id,status FROM tasks WHERE fingerprint=? AND id<>?", (new_fp, task_id))
    if fp_changed and dup:
        raise ValueError(f"修改后的业务指纹已存在任务 #{dup['id']}（{dup['status']}），不能重复建单")

    payload = {}
    try:
        import json
        payload = json.loads(task["payload"] or "{}")
    except Exception:
        payload = {}
    payload.update({k: v for k, v in allowed.items()})

    with write_tx() as c:
        if fp_changed:
            c.execute("UPDATE tasks SET status='cancelled', claim_token=NULL, updated_at=? WHERE id=?",
                      (_iso(_now()), task_id))
            c.execute(
                "INSERT INTO tasks(enterprise_id,window,action,employee_name,id_card,social_no,"
                "base_before,base_after,effective_month,payload,fingerprint,status,batch_id,updated_at,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending',?,?,?)",
                (task["enterprise_id"], task["window"], task["action"],
                 allowed.get("employee_name", task["employee_name"]), id_card,
                 allowed.get("social_no", task["social_no"]),
                 allowed.get("base_before", task["base_before"]),
                 allowed.get("base_after", task["base_after"]), month,
                 __import__("json").dumps(payload, ensure_ascii=False), new_fp,
                 task["batch_id"], _iso(_now()), _iso(_now())))
            new_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        else:
            c.execute(
                "UPDATE tasks SET employee_name=?, id_card=?, social_no=?, base_before=?, base_after=?, "
                "effective_month=?, payload=?, status='pending', attempts=0, claim_token=NULL, "
                "error_code=NULL, error_message=NULL, error_meta=NULL, checkpoint='{}', "
                "next_retry_at=NULL, last_stage='fixed', updated_at=? WHERE id=?",
                (allowed.get("employee_name", task["employee_name"]), id_card,
                 allowed.get("social_no", task["social_no"]),
                 allowed.get("base_before", task["base_before"]),
                 allowed.get("base_after", task["base_after"]), month,
                 __import__("json").dumps(payload, ensure_ascii=False), _iso(_now()), task_id))
            new_id = task_id
    orch.audit(actor, "task_fixed", new_id, task["enterprise_id"],
               {"changes": allowed, "fingerprint_changed": fp_changed, "old_id": task_id})
    return {"task_id": new_id, "fingerprint_changed": fp_changed}


def requeue(task_id: int, actor: str = "operator", reset_attempts: bool = True) -> None:
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] == "success":
        raise ValueError("已成功的任务不能重跑（幂等保护）")
    with write_tx() as c:
        c.execute(
            "UPDATE tasks SET status='pending', claim_token=NULL, error_code=NULL, error_message=NULL, "
            "error_meta=NULL, next_retry_at=NULL, attempts=?, checkpoint=CASE WHEN ? THEN '{}' ELSE checkpoint END, "
            "updated_at=? WHERE id=?",
            (0 if reset_attempts else task["attempts"], 1 if reset_attempts else 0,
             _iso(_now()), task_id))
    orch.audit(actor, "task_requeued", task_id, task["enterprise_id"], {"reset_attempts": reset_attempts})


def skip(task_id: int, actor: str = "operator", reason: str = "") -> None:
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] == "success":
        raise ValueError("已成功的任务不能跳过")
    with write_tx() as c:
        c.execute("UPDATE tasks SET status='skipped', claim_token=NULL, error_message=?, "
                  "error_code='already_exists', completed_at=?, updated_at=? WHERE id=?",
                  (reason or "人工确认跳过", _iso(_now()), _iso(_now()), task_id))
    orch.audit(actor, "task_skipped", task_id, task["enterprise_id"], {"reason": reason})


def cancel(task_id: int, actor: str = "operator", reason: str = "") -> None:
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] in ("success", "submitting"):
        raise ValueError("已提交/已成功的任务不能取消")
    with write_tx() as c:
        c.execute("UPDATE tasks SET status='cancelled', claim_token=NULL, error_message=?, updated_at=? "
                  "WHERE id=?", (reason or "人工取消", _iso(_now()), task_id))
    orch.audit(actor, "task_cancelled", task_id, task["enterprise_id"], {"reason": reason})


def error_groups() -> list[dict]:
    """异常处理页按分类聚合。"""
    rs = rows("SELECT error_code, COUNT(*) n FROM tasks WHERE error_code IS NOT NULL "
              "AND (status IN ('needs_fix','failed','retry','unknown') "
              "     OR error_code='quota_exceeded') GROUP BY error_code")
    out = []
    for r in rs:
        meta = public(r["error_code"])
        out.append({**meta, "count": r["n"]})
    out.sort(key=lambda x: -x["count"])
    return out
