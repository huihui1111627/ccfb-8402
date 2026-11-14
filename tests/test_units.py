"""纯单元测试：指纹、花名册解析、错误归类、幂等 claim。"""
from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from app.domain import fingerprint as fp_fn
from app.roster import parse_roster, validate_id_card
from app.orchestrator import classify_error


def test_fingerprint_is_stable_and_excludes_amounts():
    a = fp_fn("c001", "SI", "110101199001010015", "add", "2026-11")
    b = fp_fn(" C001 ", "si", "110101199001010015", "ADD", "2026-11")
    assert a == b
    # 金额不参与指纹：同一人同月同险种调基只能申报一次
    assert a == fp_fn("C001", "si", "110101199001010015", "add", "2026-11")
    assert a != fp_fn("C001", "si", "110101199001010015", "add", "2026-12")


@pytest.mark.parametrize("raw,expect", [("增员", "add"), ("停保", "remove"), ("调基", "adjust"),
                                        ("基数变更", "adjust"), ("新增参保", "add")])
def test_action_synonyms(raw, expect):
    from app.domain import normalize_action
    assert normalize_action(raw) == expect


def test_id_card_checksum():
    assert validate_id_card("110101199001010015") is None
    assert validate_id_card("110101199001010016") == "身份证校验位不正确"
    assert validate_id_card("abc") is not None


def _wb(rows, header, extra_header_rows=()):
    wb = Workbook()
    ws = wb.active
    for r in extra_header_rows:
        ws.append(r)
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


HEADER = ["企业编码", "企业名称", "姓名", "证件号码", "社保编号", "险种", "变更类型",
          "生效月份", "原基数", "新基数", "手机号", "备注"]


def test_parse_roster_detects_header_and_actions():
    data = _wb([
        ["C001", "远航", "周洁", "110101199909090094", "", "社保", "增员", "2026-11", "", 5000, "", ""],
        ["C001", "远航", "张伟", "110101199001010015", "SIC001001", "社保", "基数调整", "2026-11", 12000, 13500, "", ""],
        ["C002", "青禾", "陈静", "110101199505050053", "SIC002002", "社保", "", "2026-11", 8000, "", "", ""],
    ], HEADER, extra_header_rows=[["11月花名册"], ["制表：业务部"]])
    batch = parse_roster(data, "x.xlsx")
    assert len(batch.rows) == 3
    assert batch.rows[0].row_no == 4
    assert [r.action for r in batch.rows] == ["add", "adjust", "remove"]


def test_parse_roster_infers_action_when_blank():
    for i, (before, after, expected) in enumerate([("8000", "", "remove"),
                                                   ("11000", "12500", "adjust"),
                                                   ("", "5000", "add")]):
        idc = ["110101199909090094", "110101199808080081", "110101199010100100"][i]
        data = _wb([["C001", "远航", "某", idc, "", "社保", "", "2026-11", before, after, "", ""]], HEADER)
        b = parse_roster(data, "x.xlsx")
        assert b.rows[0].action == expected, (before, after, b.rows[0].action)


def test_parse_roster_quarantines_bad_rows():
    data = _wb([
        ["C001", "远航", "好人", "110101199909090094", "", "社保", "增员", "2026-11", "", 5000, "", ""],
        ["C001", "远航", "坏人", "110101199001010016", "", "社保", "增员", "2026-11", "", 5000, "", ""],
        ["C001", "远航", "缺月", "110101199001010015", "", "社保", "增员", "", "", 5000, "", ""],
        ["", "", "缺企业", "110101199001010015", "", "社保", "增员", "2026-11", "", 5000, "", ""],
    ], HEADER)
    b = parse_roster(data, "x.xlsx")
    assert len(b.rows) == 1
    reasons = " | ".join(i.reason for i in b.issues)
    assert "校验位" in reasons and "生效月份" in reasons and "企业编码" in reasons


def test_alias_columns():
    header = ["客户编码", "客户名称", "员工姓名", "身份证号", "申报业务", "业务类型",
              "所属期", "申报基数"]
    data = _wb([["C001", "远航", "周洁", "110101199909090094", "社会保险", "参保", "2026/11", 5000.0]], header)
    b = parse_roster(data, "x.xlsx")
    assert b.rows[0].window == "si"
    assert b.rows[0].action == "add"
    assert b.rows[0].effective_month == "2026-11"


@pytest.mark.parametrize("text,expect", [
    ("【E201】证件号码格式错误", "id_card_invalid"),
    ("【E205】姓名与证件号码不匹配", "name_mismatch"),
    ("【E301】缴费基数超出规定范围", "base_out_of_range"),
    ("【E401】该人员在当前单位已处于参保状态", "already_exists"),
    ("【E402】该人员不在当前单位参保名册中", "not_found"),
    ("【E500】当前访问人数过多，请稍后再试", "rate_limited"),
    ("会话已过期，请重新登录", "session_expired"),
    ("访问人数过多", "rate_limited"),
    ("some unknown error", "site_error"),
    (None, "site_error"),
])
def test_classify_error(text, expect):
    code, _ = classify_error(text)
    assert code == expect
