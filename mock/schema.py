"""Mock 政务站点的数据表与种子数据。

与业务库共用同一个 SQLite 文件，但表名以 mock_ 前缀隔离。
真实部署时这两套系统不会同库，这里只是本地演示方便。
"""
from __future__ import annotations

import sqlite3

from app.seed import EMPLOYEES, ENTERPRISES, NEW_HIRES

SCHEMA = """
CREATE TABLE IF NOT EXISTS mock_enterprises (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    si_account TEXT NOT NULL,
    si_password TEXT NOT NULL,
    hf_account TEXT NOT NULL,
    hf_password TEXT NOT NULL,
    phone TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enterprise_code TEXT NOT NULL,
    name TEXT NOT NULL,
    id_card TEXT NOT NULL,
    social_no TEXT,
    base REAL,
    status TEXT NOT NULL DEFAULT 'active',
    UNIQUE(enterprise_code, id_card)
);

CREATE TABLE IF NOT EXISTS mock_sessions (
    sid TEXT PRIMARY KEY,
    window TEXT NOT NULL,
    enterprise_code TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_sms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    code TEXT NOT NULL,
    purpose TEXT NOT NULL,
    created_at REAL NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mock_drafts (
    draft_id TEXT PRIMARY KEY,
    window TEXT NOT NULL,
    enterprise_code TEXT NOT NULL,
    action TEXT NOT NULL,
    values_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window TEXT NOT NULL,
    enterprise_code TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    action TEXT NOT NULL,
    employee_name TEXT NOT NULL,
    id_card TEXT NOT NULL,
    social_no TEXT,
    base_before REAL,
    base_after REAL,
    effective_month TEXT NOT NULL,
    receipt_no TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    UNIQUE(window, enterprise_code, fingerprint)
);

CREATE TABLE IF NOT EXISTS mock_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    path TEXT NOT NULL,
    status INTEGER NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "session_ttl_seconds": "600",
    "fixed_sms_code": "",          # 留空表示必须从短信网关取真实验证码
    "submit_latency_ms": "0",
    "fail_next_submit": "0",       # 提交前返回 500（未落库）
    "drop_next_submit_response": "0",  # 提交已落库但响应丢失（模拟超时）
    "rate_limit_per_min": "0",     # 0 = 不限制
    "rate_limit_window_seconds": "60",
    "maintenance": "0",
    "expire_on_step": "0",         # 在第 N 步提交后强制让会话失效（测试续跑）
}


def init_mock_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for key, val in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO mock_settings(key,value) VALUES (?,?)", (key, val))
    cur = conn.execute("SELECT COUNT(*) FROM mock_enterprises")
    if cur.fetchone()[0] == 0:
        for code, name, si_acc, si_pwd, hf_acc, hf_pwd, phone, *_ in ENTERPRISES:
            conn.execute(
                "INSERT INTO mock_enterprises(code,name,si_account,si_password,hf_account,hf_password,phone) "
                "VALUES (?,?,?,?,?,?,?)",
                (code, name, si_acc, si_pwd, hf_acc, hf_pwd, phone),
            )
    cur = conn.execute("SELECT COUNT(*) FROM mock_employees")
    if cur.fetchone()[0] == 0:
        for code, name, idc, social_no, base, status in EMPLOYEES:
            conn.execute(
                "INSERT INTO mock_employees(enterprise_code,name,id_card,social_no,base,status) "
                "VALUES (?,?,?,?,?,?)",
                (code, name, idc, social_no, base, status),
            )
        # 新人：未在册，但预置姓名/证件号用于「姓名不符」校验
        for code, name, idc, social_no, base in NEW_HIRES:
            conn.execute(
                "INSERT INTO mock_employees(enterprise_code,name,id_card,social_no,base,status) "
                "VALUES (?,?,?,?,?, 'inactive')",
                (code, name, idc, social_no, base),
            )


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM mock_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO mock_settings(key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
