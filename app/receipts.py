"""回执归档：站点回执页 PDF + 中文封面 + 哈希链台账。

为什么要有封面：真实站点回执页往往不能完整打印，且 RPA 需要把「谁、何时、
用哪份花名册、由哪个 worker、第几次尝试」一起固化下来，方便对账和审计。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from .config import settings
from .domain import ACTIONS, WINDOWS

_FONT = "STSong-Light"
_font_ready = False


def _ensure_font() -> None:
    global _font_ready
    if not _font_ready:
        pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
        _font_ready = True


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_cover(task: dict, enterprise: dict, receipt_no: str, screenshots: list[str],
                execution_id: int | None) -> bytes:
    _ensure_font()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 60
    c.setFont(_FONT, 20)
    c.drawCentredString(width / 2, y, "申报受理回执归档")
    y -= 26
    c.setFont(_FONT, 10)
    c.setFillColorRGB(0.35, 0.38, 0.42)
    c.drawCentredString(width / 2, y, "RPA 自动归档 · 本页为归档封面，后附站点原始回执")
    c.setFillColorRGB(0, 0, 0)
    y -= 34

    rows_data = [
        ("受理编号", receipt_no),
        ("申报险种", WINDOWS.get(task.get("window", ""), {}).get("name", task.get("window", ""))),
        ("申报业务", ACTIONS.get(task.get("action", ""), task.get("action", ""))),
        ("单位名称", enterprise.get("name", "")),
        ("单位编号", enterprise.get("code", "")),
        ("姓名", task.get("employee_name", "")),
        ("证件号码", task.get("id_card", "")),
        ("社保编号", task.get("social_no") or "—"),
        ("调整前基数", f"{task['base_before']:.2f} 元" if task.get("base_before") else "—"),
        ("申报基数", f"{task['base_after']:.2f} 元" if task.get("base_after") else "—"),
        ("生效月份", task.get("effective_month", "")),
        ("业务指纹", task.get("fingerprint", "")),
        ("执行批次", task.get("batch_id") or "—"),
        ("RPA 执行编号", str(execution_id) if execution_id else "—"),
        ("归档时间", _now_text()),
    ]
    c.setFont(_FONT, 11)
    label_x = 64
    value_x = 190
    for label, value in rows_data:
        c.setFillColorRGB(0.35, 0.38, 0.42)
        c.drawString(label_x, y, label)
        c.setFillColorRGB(0, 0, 0)
        text = str(value)
        if len(text) > 46:
            text = text[:45] + "…"
        c.drawString(value_x, y, text)
        y -= 22

    y -= 10
    c.setStrokeColorRGB(0.82, 0.85, 0.89)
    c.line(label_x, y, width - label_x, y)
    y -= 26
    c.setFont(_FONT, 11)
    c.drawString(label_x, y, "过程留痕")
    y -= 18
    c.setFont(_FONT, 9)
    for shot in screenshots[-12:]:
        c.setFillColorRGB(0.35, 0.38, 0.42)
        c.drawString(label_x + 8, y, "· " + Path(shot).name.lstrip("· "))
        y -= 14
        if y < 90:
            break

    y = 62
    c.setFont(_FONT, 8)
    c.setFillColorRGB(0.45, 0.48, 0.52)
    c.drawString(label_x, y + 12, "本文件由申报 RPA 系统自动生成并归档。业务指纹唯一，同一指纹不可重复申报。")
    c.drawString(label_x, y, "校验方式：比对业务指纹与站点受理编号；台账文件见 receipts/ledger.jsonl。")
    c.showPage()
    c.save()
    return buf.getvalue()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def archive(task: dict, enterprise: dict, site_pdf: bytes, receipt_no: str,
            screenshots: list[str], execution_id: int | None) -> tuple[Path, str]:
    target_dir = settings.receipts_dir / task["window"] / enterprise["code"]
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"{ts}_{receipt_no}_{task['fingerprint'][:8]}.pdf"

    cover = build_cover(task, enterprise, receipt_no, screenshots, execution_id)
    writer = PdfWriter()
    for part in (cover, site_pdf):
        reader = PdfReader(io.BytesIO(part))
        for page in reader.pages:
            writer.add_page(page)
    writer.add_metadata({
        "/Title": f"申报受理回执 {receipt_no}",
        "/Author": "申报 RPA 系统",
        "/Subject": f"{enterprise['code']} {task['employee_name']} {ACTIONS.get(task['action'], '')}",
        "/Keywords": task["fingerprint"],
    })
    tmp = path.with_suffix(".pdf.tmp")
    with tmp.open("wb") as f:
        writer.write(f)
    os.replace(tmp, path)
    sha = sha256_file(path)
    append_ledger(task, enterprise, receipt_no, path, sha, execution_id)
    return path, sha


def append_ledger(task: dict, enterprise: dict, receipt_no: str, path: Path, sha: str,
                  execution_id: int | None) -> None:
    ledger = settings.receipts_dir / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    prev = ""
    if ledger.exists():
        try:
            with ledger.open("rb") as f:
                f.seek(max(0, ledger.stat().st_size - 65536))
                tail = f.read().decode("utf-8", "ignore").strip().splitlines()
            if tail:
                prev = json.loads(tail[-1]).get("hash", "")
        except Exception:
            prev = ""
    entry = {
        "ts": _now_text(),
        "receipt_no": receipt_no,
        "fingerprint": task["fingerprint"],
        "enterprise_code": enterprise["code"],
        "task_id": task.get("id"),
        "execution_id": execution_id,
        "file": str(path.relative_to(settings.receipts_dir)),
        "sha256": sha,
        "prev": prev,
    }
    entry["hash"] = hashlib.sha256(
        json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def verify_ledger() -> tuple[int, list[str]]:
    """校验台账哈希链完整性，返回 (条数, 问题列表)。"""
    ledger = settings.receipts_dir / "ledger.jsonl"
    if not ledger.exists():
        return 0, []
    issues: list[str] = []
    prev = ""
    n = 0
    for line in ledger.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        n += 1
        if e.get("prev") != prev:
            issues.append(f"第 {n} 条 prev 哈希不连续")
        h = e.pop("hash", None)
        calc = hashlib.sha256(json.dumps(e, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if h != calc:
            issues.append(f"第 {n} 条记录内容被篡改")
        prev = h
        f = settings.receipts_dir / e["file"]
        if not f.exists():
            issues.append(f"第 {n} 条回执文件缺失：{e['file']}")
        elif sha256_file(f) != e.get("sha256"):
            issues.append(f"第 {n} 条回执文件哈希不符：{e['file']}")
    return n, issues
