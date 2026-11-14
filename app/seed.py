"""Mock 数据种子。企业账号/客户名册都在这里，便于演示与测试。

注意：这里的证件号是本地演示用的合成号码（校验位已计算），不对应真实公民。
"""
from __future__ import annotations

import sqlite3

ENTERPRISES = [
    ("C001", "示例市远航物流有限公司", "C001", "Si#2024C001", "C001", "Hf#2024C001", "13800000001", 2, 500, 10),
    ("C002", "示例市青禾餐饮管理有限公司", "C002", "Si#2024C002", "C002", "Hf#2024C002", "13800000002", 1, 300, 5),
    ("C003", "示例市启明精密制造有限公司", "C003", "Si#2024C003", "C003", "Hf#2024C003", "13800000003", 1, 800, 8),
    ("C004", "示例市云帆信息技术有限公司", "C004", "Si#2024C004", "C004", "Hf#2024C004", "13800000004", 1, 200, 3),
]

# (企业, 姓名, 证件号, 社保编号, 当前基数, 在册状态)
EMPLOYEES = [
    ("C001", "张伟", "110101199001010015", "SIC001001", 12000, "active"),
    ("C001", "李娜", "110101199202020025", "SIC001002", 15000, "active"),
    ("C001", "王强", "110101199303030038", "SIC001003", 8000, "active"),
    ("C002", "刘洋", "110101199404040040", "SIC002001", 9000, "active"),
    ("C002", "陈静", "110101199505050053", "SIC002002", 9500, "active"),
    ("C003", "杨帆", "110101199606060066", "SIC003001", 20000, "active"),
    ("C003", "赵敏", "110101199707070079", "SIC003002", 8000, "active"),
    ("C004", "孙悦", "110101199808080081", "SIC004001", 11000, "active"),
]

# 可增员的新人（尚未在册）
NEW_HIRES = [
    ("C001", "周洁", "110101199909090094", None, None),
    ("C001", "吴磊", "110101199010100100", None, None),
    ("C002", "郑爽", "110101199111110113", None, None),
    ("C002", "韩梅", "110101199403030035", None, None),
    ("C003", "冯刚", "110101199212120126", None, None),
]

# 当前社保缴费基数上限/下限（示例市，演示用）
BASE_MIN = 4000.0
BASE_MAX = 35000.0


def seed_demo_data(conn: sqlite3.Connection) -> None:
    cur = conn.execute("SELECT COUNT(*) FROM enterprises")
    if cur.fetchone()[0] > 0:
        return
    for code, name, si_acc, si_pwd, hf_acc, hf_pwd, phone, par, quota, prio in ENTERPRISES:
        conn.execute(
            "INSERT INTO enterprises(code,name,si_account,si_password,hf_account,hf_password,phone,max_parallel,monthly_quota,priority) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (code, name, si_acc, si_pwd, hf_acc, hf_pwd, phone, par, quota, prio),
        )
