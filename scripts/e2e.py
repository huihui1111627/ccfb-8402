"""端到端：真实 Playwright + 真实 HTTP mock 站点。

启动两个 uvicorn（mock 站点 :8199 / 控制台不需要），直接驱动 Engine。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port: int, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"端口 {port} 未就绪")


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rpa-e2e-"))
    port = free_port()
    env = {
        **os.environ,
        "RPA_DB": str(tmp / "rpa.db"),
        "RPA_ARTIFACTS": str(tmp / "artifacts"),
        "RPA_RECEIPTS": str(tmp / "receipts"),
        "MOCK_BASE_URL": f"http://127.0.0.1:{port}",
        "RPA_SMS_TIMEOUT_S": "20",
        "PYTHONPATH": str(ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock.server:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=str(tmp), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        wait_port(port)
        # 在子进程写入的同一个库上再起引擎
        os.environ.update({k: v for k, v in env.items() if k.startswith(("RPA_", "MOCK_"))})

        from app.db import init_db, get_conn
        init_db(seed=True)
        from app.importer import import_roster
        from app.runner import Engine

        sys.path.insert(0, str(ROOT / "scripts"))
        from make_roster import build_demo_roster
        roster = build_demo_roster()
        res = import_roster(roster.read_bytes(), roster.name)
        print("导入：", json.dumps(res.as_dict(), ensure_ascii=False))

        engine = Engine(max_concurrency=3)
        await engine.start()
        deadline = time.time() + 240
        terminal = ("success", "duplicate", "needs_fix", "failed", "skipped", "cancelled")
        while time.time() < deadline:
            st = get_conn().execute(
                "SELECT status, COUNT(*) n FROM tasks GROUP BY status").fetchall()
            done = all(r["status"] in terminal for r in st)
            if done:
                break
            await asyncio.sleep(1)
        await engine.stop()

        st = {r["status"]: r["n"] for r in get_conn().execute(
            "SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
        print("任务状态：", st)
        print("引擎统计：", engine.stats)
        for r in get_conn().execute("SELECT id,employee_name,status,error_code,error_message FROM tasks WHERE status IN ('needs_fix','failed')"):
            print("  异常：", dict(r))
        # 站点防重复
        subs = get_conn().execute(
            "SELECT window, enterprise_code, fingerprint, COUNT(*) n FROM mock_submissions "
            "GROUP BY window, enterprise_code, fingerprint HAVING n>1").fetchall()
        print("站点重复提交：", len(subs))
        receipts = list((tmp / "receipts").rglob("*.pdf"))
        print("回执 PDF：", len(receipts))
        return 0 if len(subs) == 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        out = proc.stdout.read().decode("utf-8", "ignore") if proc.stdout else ""
        if out.strip():
            print("--- mock server log ---")
            print(out[-4000:])


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
