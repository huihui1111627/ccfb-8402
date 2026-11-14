"""Excel 花名册解析与识别。

支持：
- 列名别名匹配（HR 到处导模板）
- 无变更类型列时的推断：有原基数无新基数→减员；有基数变化→调基；否则增员
- 多行表头前导说明行跳过
- 校验：证件号、生效月份、基数
- 坏行进入隔离区（quarantine），不阻塞整批
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from openpyxl import load_workbook

from .domain import COLUMN_ALIASES, fingerprint, normalize_action, normalize_window


class RosterError(Exception):
    pass


@dataclass
class RosterRow:
    row_no: int
    enterprise_code: str
    enterprise_name: str
    employee_name: str
    id_card: str
    window: str
    action: str
    effective_month: str
    social_no: str | None = None
    base_before: float | None = None
    base_after: float | None = None
    phone: str | None = None
    remark: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.enterprise_code, self.window, self.id_card,
                           self.action, self.effective_month)

    def to_payload(self) -> dict:
        return {
            "enterprise_code": self.enterprise_code,
            "enterprise_name": self.enterprise_name,
            "employee_name": self.employee_name,
            "id_card": self.id_card,
            "social_no": self.social_no,
            "base_before": self.base_before,
            "base_after": self.base_after,
            "phone": self.phone,
            "remark": self.remark,
            "effective_month": self.effective_month,
        }


@dataclass
class RosterIssue:
    row_no: int
    raw: dict[str, Any]
    reason: str


@dataclass
class RosterBatch:
    filename: str
    rows: list[RosterRow]
    issues: list[RosterIssue]
    detected_columns: dict[str, str]


def _norm_header(v: Any) -> str:
    return re.sub(r"[\s　*：:()（）]", "", str(v or "")).lower()


def _find_header_row(ws, max_scan: int = 12) -> tuple[int, list[str]]:
    header_map = {_norm_header(a) for aliases in COLUMN_ALIASES.values() for a in aliases}
    best = (0, [], 0)
    for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=min(max_scan, ws.max_row),
                                             values_only=True), start=1):
        cells = [_norm_header(c) for c in row]
        hits = sum(1 for c in cells if c and c in header_map)
        if hits > best[2]:
            best = (r_idx, cells, hits)
    if best[2] < 2:
        raise RosterError("未识别到花名册表头（至少需要姓名、证件号等列）")
    return best[0], best[1]


def _map_columns(header_cells: list[str]) -> dict[str, int]:
    norm_aliases = {key: {_norm_header(a) for a in aliases} for key, aliases in COLUMN_ALIASES.items()}
    out: dict[str, int] = {}
    for key, aliases in norm_aliases.items():
        for idx, cell in enumerate(header_cells):
            if cell in aliases:
                out.setdefault(key, idx)
                break
    return out


def _to_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, datetime):
        return v.strftime("%Y-%m")
    if isinstance(v, date):
        return v.strftime("%Y-%m")
    return str(v).strip()


def normalize_month(raw: Any) -> str | None:
    s = _to_str(raw)
    if not s:
        return None
    s = s.replace("/", "-").replace(".", "-").replace("年", "-").replace("月", "")
    m = re.match(r"^(\d{4})-?(\d{1,2})", s)
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return None
    return f"{year:04d}-{month:02d}"


ID_RE = re.compile(r"^[0-9]{18}$")


def validate_id_card(id_card: str) -> str | None:
    """支持中国二代身份证校验位；也允许统一社会信用代码形态的证件（按 18 位字母数字）。"""
    s = id_card.strip().upper()
    if re.match(r"^[0-9]{15}$", s):
        return None
    if not ID_RE.match(s):
        return "证件号不是 18 位身份证号"
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check = "10X98765432"
    total = sum(int(c) * w for c, w in zip(s[:17], weights))
    if check[total % 11] != s[17]:
        return "身份证校验位不正确"
    try:
        birth = date(int(s[6:10]), int(s[10:12]), int(s[12:14]))
    except ValueError:
        return "身份证出生日期段非法"
    if birth > date.today():
        return "身份证出生日期在未来"
    return None


def parse_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).replace(",", "").replace("￥", "").strip()
        return float(s) if s else None
    except ValueError:
        return None


def parse_roster(content: bytes, filename: str) -> RosterBatch:
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as e:  # noqa: BLE001
        raise RosterError(f"无法读取 Excel：{e}") from e

    ws = wb.active
    header_row, header_cells = _find_header_row(ws)
    colmap = _map_columns(header_cells)
    detected = {k: header_cells[v] for k, v in colmap.items()}

    missing = [k for k in ("enterprise_code", "enterprise_name", "employee_name", "id_card")
               if k not in colmap]
    if missing:
        raise RosterError(f"花名册缺少必需列：{', '.join(COLUMN_ALIASES[m][0] for m in missing)}")

    rows: list[RosterRow] = []
    issues: list[RosterIssue] = []
    seen_fp: dict[str, int] = {}

    def cell(row, key):
        idx = colmap.get(key)
        return row[idx] if idx is not None and idx < len(row) else None

    for r_idx, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True),
                                start=header_row + 1):
        if row is None or all(c in (None, "") for c in row):
            continue
        raw = {header_cells[i] if i < len(header_cells) else str(i): _to_str(c)
               for i, c in enumerate(row) if c not in (None, "")}

        enterprise_code = _to_str(cell(row, "enterprise_code")).upper()
        enterprise_name = _to_str(cell(row, "enterprise_name"))
        employee_name = _to_str(cell(row, "employee_name"))
        id_card = _to_str(cell(row, "id_card")).upper()
        month = normalize_month(cell(row, "effective_month"))
        window = normalize_window(_to_str(cell(row, "window"))) if "window" in colmap else None
        action = normalize_action(_to_str(cell(row, "action"))) if "action" in colmap else None
        base_before = parse_float(cell(row, "base_before"))
        base_after = parse_float(cell(row, "base_after"))

        def reject(reason: str):
            issues.append(RosterIssue(r_idx, raw, reason))

        if not enterprise_code:
            reject("企业编码为空")
            continue
        if not employee_name:
            reject("姓名为空")
            continue
        if not id_card:
            reject("证件号为空")
            continue
        bad = validate_id_card(id_card)
        if bad:
            reject(bad)
            continue
        if not month:
            reject("生效月份缺失或格式错误（应为 YYYY-MM）")
            continue

        # 未给险种时按业务动作推断：默认社保；只有公积金基数调整才明显
        if not window:
            window = "si"
        if not action:
            # 列存在但为空：有原基数且无新基数视为减员；有基数变化视为调基
            has_before = "base_before" in colmap and cell(row, "base_before") not in (None, "")
            if base_after is None and (has_before or base_before is not None):
                action = "remove"
            elif base_after is not None and base_before is not None and base_before != base_after:
                action = "adjust"
            elif base_after is not None:
                action = "add"
            else:
                action = "add"

        if action == "adjust":
            if base_after is None:
                reject("基数调整缺少新基数")
                continue
            if base_after <= 0:
                reject("新基数必须大于 0")
                continue
        if action == "remove" and not _to_str(cell(row, "social_no")):
            # 减员通常可用证件号定位，但不强求社保编号
            pass

        rr = RosterRow(
            row_no=r_idx,
            enterprise_code=enterprise_code,
            enterprise_name=enterprise_name,
            employee_name=employee_name,
            id_card=id_card,
            window=window,
            action=action,
            effective_month=month,
            social_no=_to_str(cell(row, "social_no")) or None,
            base_before=base_before,
            base_after=base_after,
            phone=_to_str(cell(row, "phone")) or None,
            remark=_to_str(cell(row, "remark")) or None,
        )
        fp = rr.fingerprint
        if fp in seen_fp:
            rr.warnings.append(f"与第 {seen_fp[fp]} 行业务指纹重复，导入时将合并")
        else:
            seen_fp[fp] = r_idx
        rows.append(rr)

    return RosterBatch(filename=filename, rows=rows, issues=issues, detected_columns=detected)
