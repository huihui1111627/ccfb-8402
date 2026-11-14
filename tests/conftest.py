"""测试夹具：每个测试一个独立 SQLite 库，并起一个真实 HTTP mock 站点。

不用 FastAPI TestClient，因为 RPA 引擎需要真实的网络端口（Playwright 走浏览器）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port: int, timeout: float = 40) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return
        except OSError:
            time.sleep(0.15)
    raise RuntimeError(f"端口 {port} 未就绪")


@pytest.fixture(scope="session")
def mock_server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mock")
    port = _free_port()
    env = {**os.environ,
           "RPA_DB": str(tmp / "rpa.db"),
           "PYTHONPATH": str(ROOT),
           "RPA_HEADLESS": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock.server:app", "--port", str(port),
         "--log-level", "error"],
        cwd=str(tmp), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        wait_port(port)
        yield {"base": f"http://127.0.0.1:{port}", "db": str(tmp / "rpa.db"), "tmp": tmp}
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def env(mock_server, tmp_path, monkeypatch):
    """切换到独立库与目录。注意：mock 站点进程仍用自己的库，
    所以这里通过一个共享库路径让两边一致。"""
    db = mock_server["db"]
    monkeypatch.setenv("RPA_DB", db)
    monkeypatch.setenv("MOCK_BASE_URL", mock_server["base"])
    monkeypatch.setenv("RPA_ARTIFACTS", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RPA_RECEIPTS", str(tmp_path / "receipts"))
    monkeypatch.setenv("RPA_ROSTER_IN", str(tmp_path / "roster_in"))
    monkeypatch.setenv("RPA_ROSTER_OUT", str(tmp_path / "roster_out"))
    monkeypatch.setenv("RPA_SMS_TIMEOUT_S", "20")
    monkeypatch.setenv("RPA_RECONCILE_DELAY_S", "0.2")
    monkeypatch.setenv("RPA_RECONCILE_QUERIES", "2")
    monkeypatch.setenv("RPA_BACKOFF_BASE_S", "1")
    monkeypatch.setenv("RPA_BACKOFF_MAX_S", "3")
    monkeypatch.setenv("RPA_RATE_LIMIT_FLOOR_S", "1")
    # 重新加载 db / config，让路径与 settings 生效
    import importlib
    import app.config
    import app.db
    importlib.reload(app.config)
    importlib.reload(app.db)
    # 关闭线程本地连接
    if hasattr(app.db._local, "conn"):
        try:
            app.db._local.conn.close()
        except Exception:
            pass
        del app.db._local.conn
    app.db.init_db(seed=True)
    # 子模块不重新加载：它们内部引用 app.db 模块对象，db 重载后仍指向新模块的
    # 函数（因为 import 的是模块而非 from-import 函数）。settings 由 app.config 重载。
    import app.orchestrator, app.flow, app.runner, app.errors, app.receipts, app.sms  # noqa: F401
    yield mock_server
    if hasattr(app.db._local, "conn"):
        try:
            app.db._local.conn.close()
        except Exception:
            pass
        del app.db._local.conn


@pytest.fixture()
def db(env):
    from app.db import get_conn
    return get_conn()


def mock_setting(key: str, value: str) -> None:
    """直接改 mock 站点库里的开关（两边共享同一个 db 文件）。"""
    from app.db import get_conn
    c = get_conn()
    c.execute("INSERT INTO mock_settings(key,value) VALUES (?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def reset_mock() -> None:
    from app.db import get_conn
    c = get_conn()
    for t in ("mock_submissions", "mock_sessions", "mock_drafts", "mock_sms", "mock_sms_outbox",
              "batch_tasks"):
        c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM tasks")
    c.execute("DELETE FROM executions")
    c.execute("DELETE FROM quarantine")
    c.execute("DELETE FROM audit_events")
    c.execute("UPDATE mock_employees SET status='active', base=base WHERE social_no IS NOT NULL")
    c.execute("UPDATE mock_employees SET status='inactive', base=NULL WHERE social_no IS NULL")
    c.execute("UPDATE mock_settings SET value='600' WHERE key='session_ttl_seconds'")
    c.execute("UPDATE mock_settings SET value='0' WHERE key IN "
              "('fail_next_submit','drop_next_submit_response','rate_limit_per_min','maintenance')")
