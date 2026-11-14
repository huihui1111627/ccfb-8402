"""任务编排与幂等存储。

这里的函数是纯数据层的，不启动浏览器。关键不变量：

1. 一个业务指纹在同一时刻只允许一个 claim（部分唯一索引兜底）。
2. 只有「待执行/待重试」的 claim 能被抢；running 的 claim 不会因为进程重启被
   另一个 worker 抢走——它会先被 reconcile 成 unknown，再按站点核对结果。
3. 提交前必须把 claim 写成 running + stage=submitting，提交成功才写 success。
   进程崩溃在 submitting 阶段的任务，恢复时一律走「核对优先」路径。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from .db import write_tx
from .domain import ACTIVE_STATUSES, ERROR_CATEGORIES, fingerprint

LEASE_SECONDS = 900  # 15 分钟；比正常单任务执行时间大一倍以上


def _now() -> str:
    """统一使用本地时间（控制台、月配额、花名册都按本地时区）。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def audit(actor: str, action: str, task_id: int | None = None,
          enterprise_id: int | None = None, detail: Any = None) -> None:
    with write_tx() as c:
        c.execute("INSERT INTO audit_events(ts,actor,action,task_id,enterprise_id,detail) "
                  "VALUES (?,?,?,?,?,?)",
                  (_now(), actor, action, task_id, enterprise_id,
                   json.dumps(detail, ensure_ascii=False) if detail is not None else None))


def create_task(conn: sqlite3.Connection, enterprise_id: int, window: str, action: str,
                employee_name: str, id_card: str, effective_month: str, payload: dict,
                batch_id: str | None = None, social_no: str | None = None,
                base_before: float | None = None, base_after: float | None = None,
                priority: int = 0) -> tuple[int, str]:
    """插入任务。返回 (task_id, state)，state ∈ created / merged / duplicate_success。

    同一指纹已存在时：
    - 已有成功/已提交记录 → duplicate_success，不新建任务（事故防线第一层）
    - 已有未完成任务 → merged，复用原任务
    - 已有 quarantine 记录（无 claim）→ 视为全新
    """
    ent = conn.execute("SELECT code FROM enterprises WHERE id=?", (enterprise_id,)).fetchone()
    if not ent:
        raise ValueError("enterprise not found")
    fp = fingerprint(ent["code"], window, id_card, action, effective_month)

    existing = conn.execute(
        "SELECT * FROM tasks WHERE fingerprint=? ORDER BY id DESC LIMIT 1", (fp,)).fetchone()
    if existing:
        if existing["status"] in ("success", "duplicate"):
            return existing["id"], "duplicate_success"
        if existing["status"] in ACTIVE_STATUSES:
            merged_payload = dict(payload)
            merged_payload["merged_from"] = existing["id"]
            conflicts: list[str] = []
            if existing["base_after"] is not None and base_after is not None \
                    and abs(float(existing["base_after"]) - float(base_after)) > 0.01:
                conflicts.append(f"基数：原任务 {existing['base_after']:g} / 本行 {base_after:g}")
            if conflicts:
                merged_payload["merge_conflicts"] = conflicts
            conn.execute(
                "UPDATE tasks SET payload=?, base_after=COALESCE(base_after, ?), "
                "base_before=COALESCE(base_before, ?), batch_id=COALESCE(batch_id, ?), "
                "updated_at=? WHERE id=?",
                (json.dumps(merged_payload, ensure_ascii=False), base_after, base_before,
                 batch_id, _now(), existing["id"]))
            return existing["id"], ("merged_conflict" if conflicts else "merged")
        # 失败/待修正/已取消：允许用户显式重跑时复用，这里重新排队
        conn.execute(
            "UPDATE tasks SET status='pending', error_code=NULL, error_message=NULL, error_meta=NULL, "
            "payload=?, base_after=COALESCE(?, base_after), base_before=COALESCE(?, base_before), "
            "batch_id=COALESCE(batch_id, ?), next_retry_at=NULL, updated_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), base_after, base_before, batch_id,
             _now(), existing["id"]))
        return existing["id"], "requeued"

    cur = conn.execute(
        "INSERT INTO tasks(enterprise_id,window,action,employee_name,id_card,social_no,"
        "base_before,base_after,effective_month,payload,fingerprint,status,batch_id,updated_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending',?,?,?)",
        (enterprise_id, window, action, employee_name, id_card, social_no, base_before,
         base_after, effective_month, json.dumps(payload, ensure_ascii=False), fp, batch_id,
         _now(), _now()))
    return int(cur.lastrowid), "created"


def claim_task(task_id: int, worker: str) -> tuple[bool, str]:
    """抢占任务。返回 (ok, reason)。"""
    with write_tx() as c:
        t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            return False, "not_found"
        if t["claim_token"] and t["status"] in ("running",):
            # 有活跃 lease 的任务不抢
            return False, "claimed_by_other"
        if t["status"] in ("success", "duplicate"):
            return False, "already_done"

        token = uuid.uuid4().hex
        try:
            cur = c.execute(
                "UPDATE tasks SET status='running', claim_token=?, claimed_at=?, attempts=attempts+1, "
                "last_stage='init', error_code=NULL, error_message=NULL, updated_at=? "
                "WHERE id=? AND (claim_token IS NULL OR claim_token=?) "
                "AND status IN ('pending','queued','retry','unknown','needs_fix','failed','cancelled')",
                (token, _now(), _now(), task_id, t["claim_token"]))
        except sqlite3.IntegrityError:
            return False, "fingerprint_claimed_elsewhere"
        if cur.rowcount != 1:
            return False, "race_lost"
        return True, token


def release_claim(task_id: int, status: str, *, error_code: str | None = None,
                  error_message: str | None = None, error_meta: dict | None = None,
                  next_retry_at: str | None = None, receipt_no: str | None = None,
                  receipt_path: str | None = None, last_stage: str | None = None,
                  defer: bool = False) -> None:
    """释放 claim。defer=True 时只改状态与延后时间，不消耗尝试次数（用于配额等外部约束）。"""
    with write_tx() as c:
        c.execute(
            "UPDATE tasks SET status=?, claim_token=NULL, error_code=?, error_message=?, error_meta=?, "
            "next_retry_at=?, receipt_no=COALESCE(?, receipt_no), receipt_path=COALESCE(?, receipt_path), "
            "last_stage=COALESCE(?, last_stage), updated_at=?, "
            "attempts=CASE WHEN ? THEN MAX(attempts-1, 0) ELSE attempts END, "
            "completed_at=CASE WHEN ? IN ('success','duplicate','skipped') THEN ? ELSE completed_at END "
            "WHERE id=?",
            (status, error_code, error_message,
             json.dumps(error_meta, ensure_ascii=False) if error_meta else None,
             next_retry_at, receipt_no, receipt_path, last_stage, _now(),
             1 if defer else 0, status, _now(), task_id))


def mark_submitting(task_id: int, checkpoint: dict) -> None:
    """提交动作落库：调用后即使进程崩溃，恢复路径也会先核对。"""
    with write_tx() as c:
        c.execute("UPDATE tasks SET last_stage='submitting', checkpoint=?, submitted_at=?, updated_at=? "
                  "WHERE id=?", (json.dumps(checkpoint, ensure_ascii=False), _now(), _now(), task_id))


def mark_submitted(task_id: int, receipt_no: str, receipt_path: str | None,
                   checkpoint: dict) -> None:
    with write_tx() as c:
        c.execute(
            "UPDATE tasks SET status='success', claim_token=NULL, receipt_no=?, receipt_path=?, "
            "checkpoint=?, error_code=NULL, error_message=NULL, error_meta=NULL, "
            "last_stage='done', completed_at=?, updated_at=? WHERE id=?",
            (receipt_no, receipt_path, json.dumps(checkpoint, ensure_ascii=False), _now(), _now(), task_id))


def save_checkpoint(task_id: int, checkpoint: dict, stage: str) -> None:
    with write_tx() as c:
        c.execute("UPDATE tasks SET checkpoint=?, last_stage=?, updated_at=? WHERE id=?",
                  (json.dumps(checkpoint, ensure_ascii=False), stage, _now(), task_id))


def reconcile_orphans() -> int:
    """进程重启后，把仍挂 running 的任务标成 unknown，等待核对。

    不直接置 pending：未知阶段可能已经提交成功，必须先去站点查。
    """
    with write_tx() as c:
        cur = c.execute(
            "UPDATE tasks SET status='unknown', claim_token=NULL, "
            "error_code='submit_unknown', error_message='进程中断，提交状态待核对', updated_at=? "
            "WHERE status='running'", (_now(),))
        n = cur.rowcount
        c.execute(
            "UPDATE tasks SET status='retry', error_code='submit_unknown', "
            "error_message='进程中断，未进入提交阶段', updated_at=? "
            "WHERE status='unknown' AND last_stage!='submitting'", (_now(),))
    return n


def find_duplicates(conn: sqlite3.Connection | None = None) -> list[dict]:
    """审计：同一指纹出现多条成功记录即事故。正常情况下应为空。"""
    from .db import get_conn
    c = conn or get_conn()
    r = c.execute(
        "SELECT fingerprint, COUNT(*) n FROM mock_submissions GROUP BY window, enterprise_code, fingerprint "
        "HAVING n>1").fetchall()
    return [dict(x) for x in r]


def classify_error(text: str | None) -> tuple[str, str]:
    """把站点文案归类。先看显式错误码 E2xx/E3xx/E4xx，再看关键词。"""
    import re
    if not text:
        return "site_error", "站点未返回可识别错误"
    m = re.search(r"【E(\d{3})】", text)
    code = m.group(1) if m else ""
    mapping = {
        "102": "invalid_field", "103": "invalid_field", "104": "invalid_field",
        "105": "invalid_field",
        "201": "id_card_invalid",
        "203": "account_mismatch",
        "205": "name_mismatch", "206": "not_found",
        "301": "base_out_of_range",
        "401": "already_exists", "402": "not_found", "403": "not_found",
        "500": "rate_limited", "900": "site_error",
    }
    if code in mapping:
        return mapping[code], text
    kw = [
        ("会话|登录|过期|超时未操作", "session_expired"),
        ("访问人数过多|限流|频率", "rate_limited"),
        ("已参保|重复增员|已处于参保", "already_exists"),
        ("未查询到|不在.*名册|不存在", "not_found"),
        ("基数|上下限|范围", "base_out_of_range"),
        ("证件|身份证", "id_card_invalid"),
        ("姓名.*不匹配|不一致", "name_mismatch"),
        ("账号|单位.*不匹配", "account_mismatch"),
    ]
    for pat, cat in kw:
        if re.search(pat, text):
            return cat, text
    return "site_error", text


def error_public(code: str) -> dict:
    meta = ERROR_CATEGORIES.get(code, ERROR_CATEGORIES["site_error"])
    return {"code": code, **meta}
