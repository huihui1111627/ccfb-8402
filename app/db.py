"""数据库连接与 schema 初始化。

使用标准库 sqlite3，所有写入通过短事务 + 显式锁完成。
WAL 模式允许 mock 站点与 RPA 引擎同时读写。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def db_path() -> Path:
    """动态读取，方便测试用环境变量切换库文件。"""
    return Path(__import__("os").environ.get("RPA_DB", DATA_DIR / "rpa.db"))

_local = threading.local()


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS enterprises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    si_account TEXT NOT NULL,
    si_password TEXT NOT NULL,
    hf_account TEXT NOT NULL,
    hf_password TEXT NOT NULL,
    si_city TEXT NOT NULL DEFAULT '示例市',
    hf_city TEXT NOT NULL DEFAULT '示例市',
    phone TEXT NOT NULL,
    max_parallel INTEGER NOT NULL DEFAULT 1,
    monthly_quota INTEGER NOT NULL DEFAULT 500,
    priority INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enterprise_id INTEGER NOT NULL REFERENCES enterprises(id),
    window TEXT NOT NULL,              -- si / hf
    action TEXT NOT NULL,              -- add / remove / adjust
    employee_name TEXT NOT NULL,
    id_card TEXT NOT NULL,
    social_no TEXT,
    base_before REAL,
    base_after REAL,
    effective_month TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    error_meta TEXT,
    last_stage TEXT,
    receipt_no TEXT,
    receipt_path TEXT,
    batch_id TEXT,
    claim_token TEXT,
    claimed_at TEXT,
    checkpoint TEXT NOT NULL DEFAULT '{}',
    reconcile_count INTEGER NOT NULL DEFAULT 0,
    submitted_at TEXT,
    completed_at TEXT,
    next_retry_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_claim ON tasks(fingerprint) WHERE claim_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status, enterprise_id);
CREATE INDEX IF NOT EXISTS ix_tasks_batch ON tasks(batch_id);
CREATE INDEX IF NOT EXISTS ix_tasks_fp ON tasks(fingerprint);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    attempt INTEGER NOT NULL,
    worker TEXT,
    stage TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at TEXT,
    screenshot_dir TEXT,
    log TEXT,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS ix_exec_task ON executions(task_id);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    filename TEXT,
    source TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    imported_by TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    task_id INTEGER,
    enterprise_id INTEGER,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS batch_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    task_id INTEGER REFERENCES tasks(id),      -- 隔离行没有任务
    row_no INTEGER,
    outcome TEXT NOT NULL DEFAULT 'created'    -- created / merged / duplicate / quarantined
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_batch_tasks_row ON batch_tasks(batch_id, row_no);
CREATE INDEX IF NOT EXISTS ix_batch_tasks_batch ON batch_tasks(batch_id);

CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT,
    row_no INTEGER,
    raw TEXT,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS rate_limit_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'normal',
    global_concurrency INTEGER NOT NULL DEFAULT 4,
    per_enterprise_concurrency INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
INSERT OR IGNORE INTO rate_limit_state(id) VALUES (1);
"""


def init_db(seed: bool = True) -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    if seed:
        from .seed import seed_demo_data
        seed_demo_data(conn)


_lock = threading.RLock()


@contextmanager
def write_tx():
    """串行化写事务，SQLite 单写者模型下避免 'database is locked'."""
    with _lock:
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def rows(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(get_conn().execute(sql, params).fetchall())


def one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


def jloads(s: str | None) -> Any:
    if not s:
        return None
    return json.loads(s)
