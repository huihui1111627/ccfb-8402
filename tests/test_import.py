"""导入与回写测试（不需要浏览器）。"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from openpyxl import load_workbook

from app.importer import import_roster


def _roster_bytes(rows=None):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["企业编码", "企业名称", "姓名", "证件号码", "社保编号", "险种", "变更类型",
               "生效月份", "原基数", "新基数", "手机号", "备注"])
    rows = rows or [
        ["C001", "远航", "周洁", "110101199909090094", "", "社保", "增员", "2026-11", "", 5000, "", ""],
        ["C001", "远航", "张伟", "110101199001010015", "SIC001001", "社保", "基数调整", "2026-11", 12000, 13500, "", ""],
        ["C002", "青禾", "郑爽", "110101199111110113", "", "公积金", "增员", "2026-11", "", 4800, "", ""],
        ["C999", "未知", "李四", "110101199302020022", "", "社保", "增员", "2026-11", "", 5000, "", ""],
        ["C001", "远航", "坏证件", "110101199001010016", "", "社保", "增员", "2026-11", "", 5000, "", ""],
    ]
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _count_tasks() -> int:
    from app.db import get_conn
    return get_conn().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _clean():
    from app.db import get_conn
    c = get_conn()
    for t in ("executions", "batch_tasks", "tasks", "quarantine", "batches", "audit_events"):
        c.execute(f"DELETE FROM {t}")


def test_import_creates_tasks_and_quarantines_bad_rows(env):
    _clean()
    res = import_roster(_roster_bytes(), "roster.xlsx")
    assert res.total == 3
    assert res.created == 3
    assert len(res.issues) == 2
    reasons = " | ".join(i["reason"] for i in res.issues)
    assert "未在系统备案" in reasons and "校验位" in reasons

    from app.db import get_conn
    quar = [dict(r) for r in get_conn().execute("SELECT * FROM quarantine")]
    assert len(quar) == 2


def test_reimport_same_roster_does_not_create_new_tasks(env):
    _clean()
    import_roster(_roster_bytes(), "roster.xlsx")
    res2 = import_roster(_roster_bytes(), "roster.xlsx")
    # 未完成的任务被复用而不是新建
    assert res2.created == 0
    assert res2.merged == 3
    assert _count_tasks() == 3


def test_reimport_after_success_is_ignored(env):
    """模拟错峰：任务已成功，客户又交了一遍同名册。必须被判重复，不建任务。"""
    _clean()
    import_roster(_roster_bytes(), "roster.xlsx")
    from app.db import write_tx
    with write_tx() as c:
        c.execute("UPDATE tasks SET status='success', receipt_no='R-1', claim_token=NULL")
    res = import_roster(_roster_bytes(), "roster.xlsx")
    assert res.duplicate == 3
    assert res.created == 0
    assert _count_tasks() == 3


def test_writeback_produces_result_workbook(env, tmp_path):
    _clean()
    res = import_roster(_roster_bytes(), "roster.xlsx")
    from app.db import get_conn, write_tx
    from app.writeback import build_result_workbook, update_roster_status
    tasks = [dict(r) for r in get_conn().execute("SELECT * FROM tasks ORDER BY id")]
    with write_tx() as c:
        c.execute("UPDATE tasks SET status='success', receipt_no='SI-1' WHERE id=?",
                  (tasks[0]["id"],))
    update_roster_status(res.batch_id, tasks[0]["fingerprint"], "success", receipt_no="SI-1")
    update_roster_status(res.batch_id, tasks[1]["fingerprint"], "needs_fix",
                         error="基数超限", error_code="base_out_of_range")
    path = build_result_workbook(res.batch_id)
    assert path and Path(path).exists()

    wb = load_workbook(path)
    ws = wb["申报结果"]
    header = [c.value for c in ws[1]]
    assert "申报状态" in header and "受理编号" in header
    rows = {r[3]: r for r in ws.iter_rows(min_row=2, values_only=True)}
    assert rows["周洁"][header.index("申报状态")] == "申报成功"
    assert rows["周洁"][header.index("受理编号")] == "SI-1"
    assert rows["张伟"][header.index("申报状态")] == "待修正"

    summary = wb["汇总"]
    values = {r[0]: r[1] for r in summary.iter_rows(min_row=1, values_only=True)}
    assert values.get("申报成功") == 1
    assert values.get("待修正") == 1


def test_original_roster_is_never_modified(env):
    import hashlib
    _clean()
    data = _roster_bytes()
    sha_in = hashlib.sha256(data).hexdigest()
    res = import_roster(data, "roster.xlsx")
    from app.writeback import IN_DIR
    stored = IN_DIR / res.batch_id / "roster.xlsx"
    assert stored.exists()
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == sha_in


def test_apply_fix_changes_fingerprint_when_identity_changes(env):
    _clean()
    import_roster(_roster_bytes(), "roster.xlsx")
    from app.db import get_conn, one
    from app.errors import apply_fix
    t = dict(get_conn().execute("SELECT * FROM tasks ORDER BY id LIMIT 1").fetchone())

    # 只改姓名：指纹不变
    r1 = apply_fix(t["id"], {"employee_name": "周洁"})
    assert r1["fingerprint_changed"] is False
    assert r1["task_id"] == t["id"]

    # 改生效月份：指纹变化，旧任务取消，新任务建单
    r2 = apply_fix(t["id"], {"effective_month": "2026-12"})
    assert r2["fingerprint_changed"] is True
    assert r2["task_id"] != t["id"]
    old = one("SELECT status FROM tasks WHERE id=?", (t["id"],))
    assert old["status"] == "cancelled"
    new = one("SELECT * FROM tasks WHERE id=?", (r2["task_id"],))
    assert new["status"] == "pending"
    assert new["effective_month"] == "2026-12"


def test_successful_task_cannot_be_refixed_or_requeued(env):
    _clean()
    import_roster(_roster_bytes(), "roster.xlsx")
    from app.db import write_tx
    from app.errors import apply_fix, requeue, skip
    with write_tx() as c:
        c.execute("UPDATE tasks SET status='success' WHERE id=1")
    with pytest.raises(ValueError):
        apply_fix(1, {"employee_name": "x"})
    with pytest.raises(ValueError):
        requeue(1)
    with pytest.raises(ValueError):
        skip(1)
