"""结果回写花名册。

原则：**只改我们自己生成的那份结果副本，绝不动客户原始文件**。
导入时把原始 Excel 存到 data/roster_in/<batch>/original.xlsx，回写生成
data/roster_out/<batch>/result.xlsx，保留原始列并追加申报结果列。
这样即使回写出错，客户源文件也不会被污染。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .config import ROOT, settings
from .db import rows
from .domain import ACTIONS, WINDOWS

IN_DIR = settings.roster_in_dir
OUT_DIR = settings.roster_out_dir

STATUS_TEXT = {
    "success": "申报成功",
    "duplicate": "已受理（幂等采纳）",
    "needs_fix": "待修正",
    "failed": "失败",
    "retry": "重试中",
    "unknown": "状态待核对",
    "running": "执行中",
    "queued": "排队中",
    "pending": "待执行",
    "cancelled": "已取消",
    "skipped": "已跳过",
}


def store_original(batch_id: str, filename: str, content: bytes) -> Path:
    d = IN_DIR / batch_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / (Path(filename).name or "roster.xlsx")
    path.write_bytes(content)
    return path


def update_roster_status(batch_id: str, fp: str, status: str, *, receipt_no: str | None = None,
                         receipt_path: str | None = None, error: str | None = None,
                         error_code: str | None = None) -> None:
    """把单条结果写入批次结果文件（按指纹定位行），并更新批次统计文件。"""
    meta_path = OUT_DIR / batch_id / "meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except Exception:
            meta = {}
    items = meta.setdefault("items", {})
    prev = items.get(fp, {})
    items[fp] = {
        **prev,
        "status": status,
        "receipt_no": receipt_no,
        "receipt_path": receipt_path,
        "error": error,
        "error_code": error_code,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")


def build_result_workbook(batch_id: str) -> Path | None:
    """生成结果 Excel：原始行 + 申报状态列。"""
    meta_path = OUT_DIR / batch_id / "meta.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text("utf-8"))
    items = meta.get("items", {})

    rows_db = rows(
        "SELECT t.*, e.code AS enterprise_code, e.name AS enterprise_name FROM tasks t "
        "JOIN enterprises e ON e.id=t.enterprise_id WHERE t.batch_id=? ORDER BY t.id", (batch_id,))

    wb = Workbook()
    ws = wb.active
    ws.title = "申报结果"
    headers = ["行号", "企业编码", "企业名称", "姓名", "证件号码", "险种", "变更类型",
               "生效月份", "申报基数", "业务指纹", "申报状态", "受理编号", "回执文件", "错误信息", "更新时间"]
    ws.append(headers)
    header_fill = PatternFill("solid", fgColor="1F4E8C")
    for cell in ws[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    color = {"success": "C6EFCE", "needs_fix": "FFEB9C", "failed": "FFC7CE",
             "duplicate": "DDEBF7"}
    for t in rows_db:
        it = items.get(t["fingerprint"], {})
        st = it.get("status", t["status"])
        ws.append([
            it.get("row_no", ""), t["enterprise_code"], t["enterprise_name"], t["employee_name"],
            t["id_card"], WINDOWS.get(t["window"], {}).get("short", t["window"]),
            ACTIONS.get(t["action"], t["action"]), t["effective_month"], t["base_after"] or "",
            t["fingerprint"], STATUS_TEXT.get(st, st), it.get("receipt_no") or t["receipt_no"] or "",
            _rel(it.get("receipt_path") or t["receipt_path"]), it.get("error") or "",
            it.get("updated_at", ""),
        ])
        fill = color.get(st)
        if fill:
            for cell in ws[ws.max_row]:
                cell.fill = PatternFill("solid", fgColor=fill)

    widths = [6, 10, 26, 10, 22, 8, 12, 10, 12, 22, 16, 22, 34, 40, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"

    # 汇总页
    summary = wb.create_sheet("汇总")
    summary.append(["批次", batch_id])
    summary.append(["源文件", meta.get("source_filename", "")])
    summary.append(["生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    summary.append([])
    summary.append(["状态", "数量"])
    counts: dict[str, int] = {}
    for t in rows_db:
        st = items.get(t["fingerprint"], {}).get("status", t["status"])
        counts[st] = counts.get(st, 0) + 1
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        summary.append([STATUS_TEXT.get(k, k), v])
    summary.append(["合计", sum(counts.values())])
    summary.column_dimensions["A"].width = 20
    summary.column_dimensions["B"].width = 40

    out = OUT_DIR / batch_id / "result.xlsx"
    wb.save(out)
    return out


def _rel(p: str | None) -> str:
    if not p:
        return ""
    try:
        return str(Path(p).relative_to(ROOT))
    except Exception:
        return str(p)
