"""花名册导入：解析 → 匹配企业 → 建任务 → 归档原始文件。"""
from __future__ import annotations

import json
import uuid

from .db import get_conn, one, write_tx
from .roster import parse_roster
from . import orchestrator as orch
from . import writeback
from .domain import ACTIONS


class ImportResult:
    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        self.total = 0
        self.created = 0
        self.merged = 0
        self.conflicts = 0
        self.duplicate = 0
        self.requeued = 0
        self.issues: list[dict] = []
        self.unknown_enterprises: set[str] = set()
        self.warnings: list[str] = []

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id, "total": self.total, "created": self.created,
            "merged": self.merged, "conflicts": self.conflicts,
            "duplicate": self.duplicate, "requeued": self.requeued,
            "issues": self.issues, "unknown_enterprises": sorted(self.unknown_enterprises),
            "warnings": self.warnings,
        }


def import_roster(content: bytes, filename: str, actor: str = "operator") -> ImportResult:
    batch = parse_roster(content, filename)
    batch_id = uuid.uuid4().hex[:12]
    res = ImportResult(batch_id)

    writeback.store_original(batch_id, filename, content)
    meta = {"source_filename": filename, "items": {}}
    (writeback.OUT_DIR / batch_id).mkdir(parents=True, exist_ok=True)
    (writeback.OUT_DIR / batch_id / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")

    ent_by_code = {}
    for r in get_conn().execute("SELECT * FROM enterprises WHERE active=1"):
        ent_by_code[r["code"].upper()] = dict(r)

    with write_tx() as c:
        c.execute("INSERT INTO batches(id,filename,source,total,created_at,imported_by) "
                  "VALUES (?,?,?,?,datetime('now','localtime'),?)",
                  (batch_id, filename, "upload", len(batch.rows), actor))

        for row in batch.rows:
            ent = ent_by_code.get(row.enterprise_code)
            if not ent:
                res.unknown_enterprises.add(row.enterprise_code)
                res.issues.append({"row_no": row.row_no, "reason": f"企业编码 {row.enterprise_code} 未在系统备案",
                                   "raw": {"姓名": row.employee_name}})
                c.execute("INSERT INTO quarantine(batch_id,row_no,raw,reason) VALUES (?,?,?,?)",
                          (batch_id, row.row_no, json.dumps(row.to_payload(), ensure_ascii=False),
                           f"企业编码 {row.enterprise_code} 未在系统备案"))
                c.execute("INSERT INTO batch_tasks(batch_id,task_id,row_no,outcome) "
                          "VALUES (?,NULL,?, 'quarantined')", (batch_id, row.row_no))
                continue

            res.total += 1
            task_id, state = orch.create_task(
                c, enterprise_id=ent["id"], window=row.window, action=row.action,
                employee_name=row.employee_name, id_card=row.id_card,
                effective_month=row.effective_month, payload=row.to_payload(),
                batch_id=batch_id, social_no=row.social_no,
                base_before=row.base_before, base_after=row.base_after)
            if state == "created":
                res.created += 1
            elif state == "merged":
                res.merged += 1
                res.warnings.append(f"第 {row.row_no} 行与已存在任务 #{task_id} 合并")
            elif state == "merged_conflict":
                res.merged += 1
                res.conflicts += 1
                task = one("SELECT payload FROM tasks WHERE id=?", (task_id,))
                detail = ""
                try:
                    detail = "；".join(json.loads(task["payload"] or "{}").get("merge_conflicts", []))
                except Exception:
                    pass
                res.warnings.append(
                    f"第 {row.row_no} 行与已存在任务 #{task_id} 合并，但基数不一致（{detail}），已保留原任务基数")
            elif state == "requeued":
                res.requeued += 1
            elif state == "duplicate_success":
                res.duplicate += 1
                res.warnings.append(
                    f"第 {row.row_no} 行（{row.employee_name}，{ACTIONS.get(row.action)}）与已成功任务 #{task_id} 重复，已忽略")

            entry = meta["items"].setdefault(row.fingerprint, {"status": "pending"})
            entry.setdefault("row_no", row.row_no)

            # 记录「本批次 -> 任务」的关联。任务可能来自更早的批次（合并/判重），
            # 仅靠 tasks.batch_id 会让本批次看起来什么都没做。
            c.execute("INSERT INTO batch_tasks(batch_id,task_id,row_no,outcome) VALUES (?,?,?,?) "
                      "ON CONFLICT(batch_id,row_no) DO UPDATE SET task_id=excluded.task_id, "
                      "outcome=excluded.outcome",
                      (batch_id, task_id, row.row_no, state))

        for issue in batch.issues:
            c.execute("INSERT INTO quarantine(batch_id,row_no,raw,reason) VALUES (?,?,?,?)",
                      (batch_id, issue.row_no, json.dumps(issue.raw, ensure_ascii=False), issue.reason))
            c.execute("INSERT INTO batch_tasks(batch_id,task_id,row_no,outcome) "
                      "VALUES (?,NULL,?, 'quarantined')", (batch_id, issue.row_no))
            res.issues.append({"row_no": issue.row_no, "reason": issue.reason})

    (writeback.OUT_DIR / batch_id / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    orch.audit(actor, "roster_imported", None, None, res.as_dict())
    return res


def generate_template() -> bytes:
    """下载空模板，列名与别名匹配。"""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "花名册"
    headers = ["企业编码", "企业名称", "姓名", "证件号码", "社保编号", "险种", "变更类型",
               "生效月份", "原基数", "新基数", "手机号", "备注"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor="1F4E8C")
    sample = [
        ["C001", "示例市远航物流有限公司", "周洁", "110101199909090094", "", "社保",
         "增员", "2026-11", "", "5000", "13900000001", ""],
        ["C001", "示例市远航物流有限公司", "张伟", "110101199001010015", "SIC001001", "社保",
         "基数调整", "2026-11", "12000", "13500", "", "年度调基"],
        ["C002", "示例市青禾餐饮管理有限公司", "刘洋", "110101199404040040", "SIC002001", "公积金",
         "减员", "2026-11", "", "", "", "离职"],
    ]
    for r in sample:
        ws.append(r)
    for i, w in enumerate([10, 30, 10, 22, 14, 8, 12, 10, 10, 10, 14, 20], start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.append([])
    ws.append(["说明：险种填 社保/公积金；变更类型填 增员/减员/基数调整；生效月份 YYYY-MM。"
               "变更类型留空时按基数自动推断。"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
