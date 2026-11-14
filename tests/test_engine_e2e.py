"""端到端引擎测试：真实 mock 站点 + 真实 Playwright。

覆盖用户最在意的几条：
1. 正常增员/减员/调基全流程跑通，回执归档。
2. 同一指纹重复导入/重复重跑 → 不产生第二次站点提交。
3. 提交响应丢失（站点已落库）→ 核对后采纳，不重提。
4. 提交前站点 500（未落库）→ 自动重试，最终成功一次。
5. 会话过期 → 自动重新登录并续跑，不重复提交。
6. 站点校验失败 → 归类为待修正，修正后重跑成功。
7. 同企业串行、多企业并行。
8. 每步截图存在。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

pytestmark = pytest.mark.asyncio


def _reset():
    from app.db import get_conn
    c = get_conn()
    for t in ("mock_submissions", "mock_sessions", "mock_drafts", "mock_sms", "mock_sms_outbox",
              "executions", "batch_tasks", "audit_events", "quarantine", "batches"):
        try:
            c.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    c.execute("DELETE FROM tasks")
    c.execute("UPDATE mock_employees SET status='active' WHERE social_no IS NOT NULL")
    c.execute("UPDATE mock_employees SET status='inactive' WHERE social_no IS NULL")
    c.execute("UPDATE mock_settings SET value='600' WHERE key='session_ttl_seconds'")
    c.execute("UPDATE mock_settings SET value='0' WHERE key IN "
              "('fail_next_submit','drop_next_submit_response','rate_limit_per_min','maintenance')")


def _setting(key: str, value: str) -> None:
    from app.db import get_conn
    get_conn().execute("INSERT INTO mock_settings(key,value) VALUES (?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _make_task(employee="周洁", id_card="110101199909090094", action="add", window="si",
               ent_code="C001", base_after=5000, month="2026-11", social_no=None,
               employee_base=None):
    from app.db import one, write_tx
    from app.orchestrator import create_task
    ent = one("SELECT * FROM enterprises WHERE code=?", (ent_code,))
    payload = {"enterprise_code": ent_code, "enterprise_name": ent["name"],
               "employee_name": employee, "id_card": id_card, "social_no": social_no,
               "base_after": base_after, "effective_month": month}
    with write_tx() as c:
        tid, state = create_task(c, ent["id"], window, action, employee, id_card, month, payload,
                                 social_no=social_no, base_after=base_after)
    return tid, state


async def _run_until_done(timeout=120, stop_on_quota=False):
    from app.db import get_conn
    from app.runner import Engine
    engine = Engine(max_concurrency=2)
    await engine.start()
    try:
        deadline = time.time() + timeout
        terminal = {"success", "duplicate", "needs_fix", "failed", "skipped", "cancelled"}
        while time.time() < deadline:
            sts = [r["status"] for r in get_conn().execute("SELECT status FROM tasks")]
            if sts and all(s in terminal for s in sts):
                break
            if stop_on_quota:
                blocked = [r for r in get_conn().execute(
                    "SELECT status FROM tasks WHERE error_code='quota_exceeded'")]
                if blocked and all(s in terminal or s == "pending" for s in sts):
                    break
            await asyncio.sleep(0.5)
        return engine
    finally:
        await engine.stop()


def _tasks():
    from app.db import get_conn
    return [dict(r) for r in get_conn().execute("SELECT * FROM tasks ORDER BY id")]


def _submissions():
    from app.db import get_conn
    return [dict(r) for r in get_conn().execute("SELECT * FROM mock_submissions ORDER BY id")]


# ---------------------------------------------------------------- 1 主流程
async def test_happy_path_add_and_receipt(env):
    _reset()
    tid, state = _make_task()
    assert state == "created"
    await _run_until_done()
    t = _tasks()[0]
    assert t["status"] == "success", t
    assert t["receipt_no"]
    subs = _submissions()
    assert len(subs) == 1
    from pathlib import Path
    assert Path(t["receipt_path"]).exists()
    # PDF 是可读的、包含中文封面和受理号
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() or "" for p in PdfReader(t["receipt_path"]).pages)
    assert "申报受理回执归档" in text
    assert t["receipt_no"] in text
    assert "远航物流" in text
    # 截图存在（RPA_ARTIFACTS 指向本测试的 tmp_path）
    from app.config import settings
    shots = list(Path(settings.artifacts_dir).rglob("task_*/**/*.png"))
    assert len(shots) >= 3, list(Path(settings.artifacts_dir).rglob("*"))[:10]


# ---------------------------------------------------------------- 2 幂等
async def test_duplicate_task_is_not_submitted_twice(env):
    _reset()
    _make_task()
    await _run_until_done()
    assert len(_submissions()) == 1

    # 再次导入同一指纹：create_task 直接识别为 duplicate_success，不建新任务
    _, state = _make_task()
    assert state == "duplicate_success"
    assert len(_tasks()) == 1

    # 强制把已成功任务改回 pending 并重跑（模拟人工误操作）
    from app.db import get_conn
    get_conn().execute("UPDATE tasks SET status='pending', receipt_no=NULL WHERE id=1")
    await _run_until_done()
    assert len(_submissions()) == 1, "站点只允许一次提交"
    t = _tasks()[0]
    assert t["status"] == "success"
    assert t["receipt_no"] == _submissions()[0]["receipt_no"]


async def test_second_task_same_fingerprint_via_requeue_checks_site(env):
    """站点已有记录但本地无任务（例如人工在站点办过）：引擎核对后直接采纳。"""
    _reset()
    # 先用一个任务正常申报
    _make_task()
    await _run_until_done()
    assert len(_submissions()) == 1
    sub = _submissions()[0]

    # 删掉本地任务，保留站点记录，重新导入
    from app.db import get_conn
    get_conn().execute("DELETE FROM executions")
    get_conn().execute("DELETE FROM tasks")
    tid, state = _make_task()
    assert state == "created"
    await _run_until_done()
    assert len(_submissions()) == 1, "站点不应出现第二条"
    t = _tasks()[0]
    assert t["status"] == "success"
    assert t["receipt_no"] == sub["receipt_no"]


# ---------------------------------------------------------------- 3 响应丢失
async def test_submit_response_lost_reconciles_without_resubmit(env):
    _reset()
    _setting("drop_next_submit_response", "1")
    _make_task()
    await _run_until_done()
    subs = _submissions()
    assert len(subs) == 1
    t = _tasks()[0]
    assert t["status"] == "success", t
    assert t["receipt_no"] == subs[0]["receipt_no"]
    assert t["attempts"] == 1, "应先核对成功，不触发第二次提交"


# ---------------------------------------------------------------- 4 提交前 500
async def test_submit_500_retries_then_succeeds_once(env):
    _reset()
    _setting("fail_next_submit", "1")
    _make_task()
    await _run_until_done()
    subs = _submissions()
    assert len(subs) == 1, f"只应成功提交一次，实际 {len(subs)}"
    t = _tasks()[0]
    assert t["status"] == "success"
    assert t["attempts"] == 2


# ---------------------------------------------------------------- 5 会话过期
async def test_session_expiry_resumes_without_duplicate(env):
    _reset()
    _setting("session_ttl_seconds", "1")
    from app.db import get_conn
    get_conn().execute("UPDATE mock_settings SET value='1' WHERE key='session_ttl_seconds'")
    _make_task()
    # 让登录→填表之间必然过期：引擎在打开申报页时会重登
    await asyncio.sleep(1.3)
    await _run_until_done()
    t = _tasks()[0]
    assert t["status"] == "success", t
    subs = _submissions()
    assert len(subs) == 1


async def test_mid_form_session_expiry_resumes_in_place(env):
    """填表到第 1 步时站点把会话踢掉：应自动重登并重放，不算一次失败尝试。"""
    _reset()
    _setting("expire_on_step", "1")
    _make_task()
    await _run_until_done()
    t = _tasks()[0]
    assert t["status"] == "success", (t["status"], t["error_code"], t["error_message"])
    assert t["attempts"] == 1, "会话过期应原地续跑，不应消耗重试次数"
    assert len(_submissions()) == 1
    # 关键：有中途过期的截图
    from app.config import settings as cfg
    from pathlib import Path as P
    names = [x.name for x in P(cfg.artifacts_dir).rglob("*.png")]
    assert any("session_expired" in n for n in names), names


async def test_session_expiry_after_submit_click_reconciles(env):
    """提交点击后站点会话失效：不能当作失败重提，必须先核对。"""
    _reset()
    _setting("expire_on_step", "3")
    _make_task()
    await _run_until_done()
    t = _tasks()[0]
    # 第 3 步提交时被踢：站点尚未落库，核对不到 → 会重排并最终成功
    assert t["status"] == "success", (t["status"], t["error_code"], t["error_message"])
    assert len(_submissions()) <= 1


# ---------------------------------------------------------------- 6 校验失败→修正重跑
async def test_validation_error_then_fix_and_rerun(env):
    _reset()
    tid, _ = _make_task(employee="张三", id_card="110101199909090094", window="hf", base_after=5200)
    await _run_until_done()
    t = _tasks()[0]
    assert t["status"] == "needs_fix"
    assert t["error_code"] == "name_mismatch"
    assert _submissions() == []

    from app.errors import apply_fix
    res = apply_fix(tid, {"employee_name": "周洁"})
    assert res["fingerprint_changed"] is False
    await _run_until_done()
    t = _tasks()[0]
    assert t["status"] == "success", (t["error_code"], t["error_message"])
    assert len(_submissions()) == 1


# ---------------------------------------------------------------- 7 并发约束
async def test_same_enterprise_serial_different_parallel(env):
    """记录每次执行的企业，断言同企业不重叠、不同企业有重叠。"""
    _reset()
    _make_task(employee="周洁", id_card="110101199909090094", ent_code="C001")
    _make_task(employee="吴磊", id_card="110101199010100100", ent_code="C001", base_after=6200)
    _make_task(employee="郑爽", id_card="110101199111110113", ent_code="C002", base_after=4800,
               window="hf")

    from app.db import get_conn
    from app.runner import Engine
    engine = Engine(max_concurrency=3)
    await engine.start()
    try:
        deadline = time.time() + 120
        while time.time() < deadline:
            sts = [r["status"] for r in get_conn().execute("SELECT status FROM tasks")]
            if sts and all(s in {"success", "needs_fix", "failed", "duplicate", "skipped"} for s in sts):
                break
            await asyncio.sleep(0.3)
    finally:
        await engine.stop()

    ex = [dict(r) for r in get_conn().execute(
        "SELECT x.*, t.enterprise_id FROM executions x JOIN tasks t ON t.id=x.task_id "
        "WHERE x.status='success' ORDER BY x.started_at")]
    # 同企业 C001 的执行区间不能重叠
    by_ent: dict[int, list] = {}
    for e in ex:
        by_ent.setdefault(e["enterprise_id"], []).append(e)
    for ent, items in by_ent.items():
        items.sort(key=lambda x: x["started_at"])
        for a, b in zip(items, items[1:]):
            assert b["started_at"] >= a["finished_at"], f"企业 {ent} 出现并发执行"
    # 至少两个企业，且不同企业的执行区间有重叠（多企业并行）
    assert len(by_ent) >= 2
    spans = [(e["started_at"], e["finished_at"], e["enterprise_id"]) for e in ex]
    overlapped = any(
        a[2] != b[2] and a[0] < b[1] and b[0] < a[1]
        for i, a in enumerate(spans) for b in spans[i + 1:])
    assert overlapped, f"不同企业未出现并行执行：{spans}"


# ---------------------------------------------------------------- 8 限流排队
async def test_peak_mode_token_bucket_queues_not_fails(env):
    """月底高峰模式：全局并发降到 2、每分钟 20 次令牌桶；任务只会排队，不会失败。"""
    _reset()
    from app.db import get_conn
    get_conn().execute("UPDATE rate_limit_state SET mode='peak' WHERE id=1")
    try:
        for i, (name, idc) in enumerate([("周洁", "110101199909090094"),
                                         ("吴磊", "110101199010100100")]):
            _make_task(employee=name, id_card=idc, ent_code="C001", base_after=5000 + i)
        from app.runner import Engine
        engine = Engine()
        engine.rate = __import__("app.runner", fromlist=["RateLimiter"]).RateLimiter(20)
        await engine.start()
        try:
            deadline = time.time() + 180
            while time.time() < deadline:
                sts = [r["status"] for r in get_conn().execute("SELECT status FROM tasks")]
                if sts and all(s in {"success", "needs_fix", "failed", "duplicate", "skipped"} for s in sts):
                    break
                await asyncio.sleep(0.3)
        finally:
            await engine.stop()
        assert engine.rate_config()[0] == 2, "高峰模式全局并发应为 2"
        t = _tasks()
        assert all(x["status"] == "success" for x in t), t
        assert len(_submissions()) == 2
    finally:
        get_conn().execute("UPDATE rate_limit_state SET mode='normal' WHERE id=1")


async def test_site_429_is_retried_not_failed(env):
    """站点限流（429）应归类为可自动重试，最终成功且只提交一次。"""
    _reset()
    _setting("rate_limit_per_min", "1")
    _setting("rate_limit_window_seconds", "2")
    # 预置一条窗口内已受理记录，让首次提交必然触发 429
    from app.db import get_conn
    import time as _t
    get_conn().execute(
        "INSERT INTO mock_submissions(window,enterprise_code,fingerprint,action,employee_name,"
        "id_card,social_no,base_before,base_after,effective_month,receipt_no,created_at) "
        "VALUES ('si','C001','seed-quota','add','占位','110101199001010015',NULL,NULL,5000,"
        "'2026-10',?,?)", ('SEEDQUOTA-' + str(int(_t.time())), _t.time()))
    _make_task()
    from app.runner import Engine
    engine = Engine(max_concurrency=1)
    await engine.start()
    try:
        deadline = time.time() + 150
        while time.time() < deadline:
            st = _tasks()[0]["status"]
            if st in {"success", "needs_fix", "failed"}:
                break
            await asyncio.sleep(0.3)
    finally:
        await engine.stop()
    t = _tasks()[0]
    assert t["status"] == "success", (t["status"], t["error_code"], t["error_message"], t["attempts"])
    assert t["attempts"] >= 2, "应先被限流再重试成功"
    assert len(_submissions()) == 2  # 1 条预置占位 + 1 条真实申报


# ---------------------------------------------------------------- 9 回执台账
async def test_ledger_hash_chain_verifies(env):
    _reset()
    for i, (name, idc) in enumerate([("周洁", "110101199909090094"), ("吴磊", "110101199010100100")]):
        _make_task(employee=name, id_card=idc, ent_code="C001", base_after=5000 + i)
    await _run_until_done()
    from app.receipts import verify_ledger
    n, issues = verify_ledger()
    assert n == 2
    assert issues == [], issues


# ---------------------------------------------------------------- 10 多引擎竞争
async def test_two_engines_racing_only_one_submits(env):
    """两个引擎实例同时处理同指纹的历史脏任务：claim 互斥 + 站点核对双保险。"""
    _reset()
    from app.db import get_conn, write_tx
    from app.runner import Engine

    ent = get_conn().execute("SELECT * FROM enterprises WHERE code='C001'").fetchone()
    payload = {"enterprise_code": "C001", "employee_name": "周洁",
               "id_card": "110101199909090094", "effective_month": "2026-11"}
    # 直接绕过 create_task 的合并逻辑，造出两条同指纹任务（模拟历史脏数据/手工插单）
    fp = __import__("app.domain", fromlist=["fingerprint"]).fingerprint(
        "C001", "si", "110101199909090094", "add", "2026-11")
    with write_tx() as c:
        for _ in range(2):
            c.execute(
                "INSERT INTO tasks(enterprise_id,window,action,employee_name,id_card,base_after,"
                "effective_month,payload,fingerprint,status,updated_at,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'pending', datetime('now','localtime'), datetime('now','localtime'))",
                (ent["id"], "si", "add", "周洁", "110101199909090094", 5000,
                 "2026-11", json.dumps(payload), fp))

    e1 = Engine(max_concurrency=2)
    e2 = Engine(max_concurrency=2)
    await e1.start()
    await e2.start()
    try:
        deadline = time.time() + 180
        while time.time() < deadline:
            sts = [r["status"] for r in get_conn().execute("SELECT status FROM tasks")]
            if sts and all(s in {"success", "needs_fix", "failed", "duplicate", "skipped"} for s in sts):
                break
            await asyncio.sleep(0.4)
    finally:
        await e1.stop()
        await e2.stop()

    subs = _submissions()
    assert len(subs) == 1, f"同指纹只应产生一次站点提交，实际 {len(subs)}"
    ts = _tasks()
    assert all(t["status"] == "success" for t in ts), ts
    assert len({t["receipt_no"] for t in ts}) == 1, [t["receipt_no"] for t in ts]


async def test_parallel_distinct_tasks_each_submit_once(env):
    """四个不同指纹的任务并行执行，各自只提交一次。"""
    _reset()
    from app.db import get_conn
    from app.runner import Engine

    pairs = [("周洁", "110101199909090094", "C001"),
             ("吴磊", "110101199010100100", "C001"),
             ("郑爽", "110101199111110113", "C002")]
    for name, idc, code in pairs:
        _make_task(employee=name, id_card=idc, ent_code=code,
                   window="hf" if code == "C002" else "si", base_after=5000)

    e1 = Engine(max_concurrency=2)
    e2 = Engine(max_concurrency=2)
    await e1.start()
    await e2.start()
    try:
        deadline = time.time() + 180
        while time.time() < deadline:
            sts = [r["status"] for r in get_conn().execute("SELECT status FROM tasks")]
            if sts and all(s in {"success", "needs_fix", "failed", "duplicate", "skipped"} for s in sts):
                break
            await asyncio.sleep(0.4)
    finally:
        await e1.stop()
        await e2.stop()

    subs = _submissions()
    fps = [s["fingerprint"] for s in subs]
    assert len(fps) == len(set(fps))
    assert len(subs) == 3, subs
    assert all(t["status"] == "success" for t in _tasks())


async def test_engine_restart_with_orphan_running_task_verifies_first(env):
    """模拟进程在提交中崩溃：重启后不得直接重提，必须先核对站点。"""
    _reset()
    from app.db import write_tx
    from app.orchestrator import reconcile_orphans
    tid, _ = _make_task()

    # 造一个「已提交但本地以为还在提交」的现场：站点已有记录
    await _run_until_done()
    sub = _submissions()[0]
    with write_tx() as c:
        c.execute("UPDATE tasks SET status='running', claim_token='stale', last_stage='submitting', "
                  "receipt_no=NULL, receipt_path=NULL WHERE id=?", (tid,))
    n = reconcile_orphans()
    assert n >= 1
    t = _tasks()[0]
    assert t["status"] in ("unknown", "retry")
    await _run_until_done()
    t = _tasks()[0]
    assert t["status"] == "success"
    assert t["receipt_no"] == sub["receipt_no"]
    assert len(_submissions()) == 1


# ---------------------------------------------------------------- 11 月配额
async def test_enterprise_monthly_quota_blocks_further_submissions(env):
    """企业月配额用满后，后续任务停在待执行并给出明确原因，不会静默失败。"""
    _reset()
    from app.db import one, write_tx
    ent = one("SELECT * FROM enterprises WHERE code='C001'")
    with write_tx() as c:
        c.execute("UPDATE enterprises SET monthly_quota=1 WHERE id=?", (ent["id"],))

    for i, (name, idc) in enumerate([("周洁", "110101199909090094"),
                                     ("吴磊", "110101199010100100")]):
        _make_task(employee=name, id_card=idc, ent_code="C001", base_after=5000 + i)

    await _run_until_done(timeout=60, stop_on_quota=True)
    ts = _tasks()
    assert len(ts) == 2
    ok = [t for t in ts if t["status"] == "success"]
    # 配额拦下的任务停在 pending/queued（调度器会把 pending 标成 queued 表示排队）
    blocked = [t for t in ts if t["error_code"] == "quota_exceeded"]
    assert len(ok) == 1, [(t["status"], t["error_code"], t["error_message"]) for t in ts]
    assert len(blocked) == 1, ts
    assert blocked[0]["error_code"] == "quota_exceeded", ts
    assert len(_submissions()) == 1

    # 调高配额后人工点「执行」即可立刻生效（next_retry_at 不影响手动触发）
    with write_tx() as c:
        c.execute("UPDATE enterprises SET monthly_quota=500 WHERE id=?", (ent["id"],))
        c.execute("UPDATE tasks SET next_retry_at=NULL WHERE status='pending'")
    await _run_until_done(timeout=90)
    assert all(t["status"] == "success" for t in _tasks())
    assert len(_submissions()) == 2
