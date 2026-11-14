"""执行队列与调度器。

并发策略
--------
- **同企业串行**：一个企业同一时刻只允许一个任务占用浏览器会话。
  真实站点通常也是单会话，且同企业并发容易触发风控。
- **多企业并行**：受全局并发度（rate_limit_state.global_concurrency）约束。
- **月底高峰**：季度/月末打开「高峰模式」后，全局并发降到 1-2，并加每分钟
  令牌桶限速；任务状态显示「排队中」。限速只影响吞吐，不影响幂等。

任务幂等
--------
每个任务在抢到 claim 后、填表之前，**先到站点按指纹查询**。查到即认为
已受理，直接采纳回执（success），绝不重复提交。这一层能兜住：
花名册重复导入、上次进程写完站点但没写库、人工在站点手动办过。
"""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from datetime import datetime, timedelta

from .config import settings
from .db import one, rows, write_tx
from .flow import (SessionExpired, SiteDriver, SiteUnavailable, SiteValidationError,
                   StepResult, SubmitOutcomeUnknown)
from . import orchestrator as orch
from . import receipts as receipt_mod
from .sms import SmsClient, SmsTimeout


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class RateLimiter:
    """全局令牌桶，按分钟补充。高峰模式自动降低配额。"""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.tokens = float(per_minute)
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    def set_rate(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.tokens = min(self.tokens, float(per_minute))

    async def acquire(self) -> None:
        if self.per_minute <= 0:
            return
        async with self.lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.updated
                self.updated = now
                self.tokens = min(float(self.per_minute), self.tokens + elapsed * self.per_minute / 60.0)
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep(max(0.05, (1 - self.tokens) * 60.0 / self.per_minute))


class Engine:
    def __init__(self, *, sms_client: SmsClient | None = None, headless: bool | None = None,
                 artifacts_dir=None, max_concurrency: int | None = None):
        self.sms = sms_client or SmsClient()
        self.headless = headless
        self.artifacts_dir = artifacts_dir
        self.max_concurrency = max_concurrency
        self.running: dict[int, asyncio.Task] = {}
        self.enterprise_busy: set[int] = set()
        self._stop = asyncio.Event()
        self._scheduler: asyncio.Task | None = None
        self.rate = RateLimiter(0)
        self.stats = {"success": 0, "failed": 0, "needs_fix": 0, "duplicate": 0, "attempts": 0}

    # ------------------------------------------------------------ 控制
    async def start(self) -> None:
        orch.reconcile_orphans()
        self._stop.clear()
        if not self._scheduler:
            self._scheduler = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._scheduler:
            self._scheduler.cancel()
            self._scheduler = None
        for t in list(self.running.values()):
            t.cancel()
        self.running.clear()

    def rate_config(self) -> tuple[int, int, int]:
        st = one("SELECT * FROM rate_limit_state WHERE id=1")
        mode = st["mode"] if st else "normal"
        global_c = self.max_concurrency or (st["global_concurrency"] if st else 4)
        per_ent = 1  # 需求：同企业串行
        per_min = 0
        if mode == "peak":
            global_c = min(global_c, 2)
            per_min = 20
        elif mode == "offpeak":
            global_c = min(global_c, 4)
            per_min = 0
        return global_c, per_ent, per_min

    # ------------------------------------------------------------ 调度
    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                global_c, per_ent, per_min = self.rate_config()
                if self.rate.per_minute != per_min:
                    self.rate.set_rate(per_min)
                self.running = {k: v for k, v in self.running.items() if not v.done()}
                capacity = global_c - len(self.running)

                if capacity > 0:
                    # 标记排队状态，让控制台能区分「待执行」与「被限流排队」
                    with write_tx() as c:
                        c.execute("UPDATE tasks SET status='queued' WHERE status='pending' "
                                  "AND id NOT IN (SELECT id FROM tasks WHERE status='running')")
                    candidates = self._pick(capacity)
                    for task_row in candidates:
                        ent_id = task_row["enterprise_id"]
                        if ent_id in self.enterprise_busy:
                            continue
                        if per_ent and len(self.running) >= global_c:
                            break
                        self.enterprise_busy.add(ent_id)
                        self.running[task_row["id"]] = asyncio.create_task(
                            self._run_one(task_row["id"], ent_id))
            except asyncio.CancelledError:
                raise
            except Exception:
                traceback.print_exc()
            await asyncio.sleep(0.4)

    def _pick(self, limit: int) -> list:
        busy = list(self.enterprise_busy)
        # 注意：NOT IN (NULL) 永远为 NULL，空列表时不能拼占位符
        busy_clause = ""
        if busy:
            busy_clause = f"AND t.enterprise_id NOT IN ({','.join('?' for _ in busy)}) "
        sql = (
            "SELECT t.* FROM tasks t JOIN enterprises e ON e.id=t.enterprise_id "
            "WHERE t.status IN ('pending','queued','retry','unknown') "
            "AND (t.next_retry_at IS NULL OR t.next_retry_at<=?) "
            f"{busy_clause}"
            "ORDER BY e.priority DESC, t.attempts ASC, t.id ASC LIMIT ?"
        )
        return rows(sql, [_now(), *busy, limit])

    # ------------------------------------------------------------ 单任务
    async def _run_one(self, task_id: int, enterprise_id: int) -> None:
        try:
            await self._execute(task_id)
        except asyncio.CancelledError:
            orch.release_claim(task_id, "retry", error_code="submit_unknown",
                               error_message="执行被中断，重新排队")
            raise
        except Exception as e:  # noqa: BLE001
            orch.release_claim(task_id, "failed", error_code="site_error",
                               error_message=f"未捕获异常：{type(e).__name__}: {e}")
            orch.audit("engine", "task_crashed", task_id, enterprise_id,
                       {"error": str(e), "trace": traceback.format_exc()[-2000:]})
        finally:
            self.enterprise_busy.discard(enterprise_id)
            self.running.pop(task_id, None)

    async def _execute(self, task_id: int) -> None:
        task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            return
        ent = one("SELECT * FROM enterprises WHERE id=?", (task["enterprise_id"],))
        if not ent:
            orch.release_claim(task_id, "failed", error_code="account_mismatch",
                               error_message="企业不存在")
            return
        if task["status"] in ("success", "duplicate"):
            return

        # 月配额：按企业统计本月已成功申报笔数，超限的企业整队暂停
        quota_block = self._quota_blocked(ent)
        if quota_block:
            # 配额问题是「等到下月或调高配额」，不是错误。延后重排，避免调度器空转刷审计。
            already = one("SELECT error_code, next_retry_at FROM tasks WHERE id=?", (task_id,))
            same = already and already["error_code"] == "quota_exceeded"
            retry_at = _quota_retry_at()
            orch.release_claim(task_id, "pending", error_code="quota_exceeded",
                               error_message=quota_block, next_retry_at=retry_at,
                               defer=True)
            if not same:
                orch.audit("engine", "quota_blocked", task_id, ent["id"],
                           {"message": quota_block, "next_retry_at": retry_at})
            return

        ok, token = orch.claim_task(task_id, "engine")
        if not ok:
            return
        self.stats["attempts"] += 1

        task = dict(one("SELECT * FROM tasks WHERE id=?", (task_id,)))
        task["attempts"] = task["attempts"]
        enterprise = dict(ent)
        payload = json.loads(task["payload"] or "{}")
        values = {
            "action": task["action"],
            "employee_name": task["employee_name"],
            "id_card": task["id_card"],
            "social_no": task["social_no"] or payload.get("social_no"),
            "base_after": task["base_after"],
            "effective_month": task["effective_month"],
            "contact_phone": payload.get("phone"),
        }
        checkpoint = json.loads(task["checkpoint"] or "{}")
        stage = checkpoint.get("stage", "start")

        await self.rate.acquire()
        exec_id = self._start_execution(task, checkpoint)

        driver = SiteDriver(window=task["window"], enterprise=enterprise, task=task,
                            sms_client=self.sms, artifacts_dir=self.artifacts_dir,
                            headless=self.headless)
        try:
            async with driver:
                await self._drive(driver, task, enterprise, values, stage, exec_id)
        except _StopExecution as stop:
            self._finish_execution(exec_id, stop.status, stop.error_code)
            return
        except (SessionExpired, SiteValidationError, SiteUnavailable, SubmitOutcomeUnknown,
                SmsTimeout) as e:
            await self._handle_known_failure(task, enterprise, e, exec_id, driver)
        except Exception as e:  # noqa: BLE001
            trace = traceback.format_exc()[-1500:]
            self._finish_execution(exec_id, "failed", "site_error")
            orch.release_claim(task_id, "failed", error_code="site_error",
                               error_message=f"{type(e).__name__}: {e}",
                               error_meta={"trace": trace})
            orch.audit("engine", "task_crashed", task_id, enterprise["id"],
                       {"error": str(e), "screenshots": driver.screenshots})

    async def _handle_known_failure(self, task: dict, enterprise: dict, exc: Exception,
                                    exec_id: int, driver: SiteDriver) -> None:
        """统一异常处置：归类 → 决定自动重试还是待修正。"""
        from .errors import classify, disposition
        task_id = task["id"]
        code, message, meta = classify(exc)
        status, next_retry = disposition(code, task["attempts"], settings.max_attempts)
        self._finish_execution(exec_id, "needs_fix" if status == "needs_fix" else "failed", code,
                               stage=task.get("last_stage"))
        orch.release_claim(task_id, status, error_code=code, error_message=message,
                           error_meta={**meta, "stage": task.get("last_stage"),
                                       "screenshots": driver.screenshots},
                           next_retry_at=next_retry, last_stage=task.get("last_stage"))
        if status == "needs_fix":
            self.stats["needs_fix"] += 1
        elif status == "failed":
            self.stats["failed"] += 1
        orch.audit("engine", f"task_{status}", task_id, enterprise["id"],
                   {"error_code": code, "message": message, "screenshots": driver.screenshots})
        if status == "needs_fix" and task.get("batch_id"):
            from .writeback import update_roster_status
            try:
                update_roster_status(task["batch_id"], task["fingerprint"], "needs_fix",
                                     error=message, error_code=code)
            except Exception:
                pass


    @staticmethod
    def _quota_blocked(ent) -> str | None:
        quota = int(ent["monthly_quota"] or 0)
        if quota <= 0:
            return None
        used = one(
            "SELECT COUNT(*) FROM tasks WHERE enterprise_id=? AND status IN ('success','duplicate') "
            "AND substr(completed_at,1,7)=?", (ent["id"], datetime.now().strftime("%Y-%m")))
        if used[0] >= quota:
            return (f"本月申报已达企业配额上限（{quota} 笔）。配额可在「企业账号」调整，"
                    f"或下月再执行。")
        return None

    def _start_execution(self, task: dict, checkpoint: dict) -> int:
        with write_tx() as c:
            cur = c.execute(
                "INSERT INTO executions(task_id,attempt,worker,stage,status,started_at,screenshot_dir,log) "
                "VALUES (?,?,?,?, 'running', ?,?,?)",
                (task["id"], task["attempts"], "engine-1", checkpoint.get("stage", "start"),
                 _now(), str(settings.artifacts_dir), None))
            return int(cur.lastrowid)

    def _finish_execution(self, exec_id: int, status: str, error_code: str | None,
                          log: dict | None = None, stage: str | None = None) -> None:
        with write_tx() as c:
            c.execute("UPDATE executions SET status=?, finished_at=?, error_code=?, log=?, "
                      "stage=COALESCE(?, stage) WHERE id=?",
                      (status, _now(), error_code, json.dumps(log or {}, ensure_ascii=False),
                       stage, exec_id))

    async def _drive(self, driver: SiteDriver, task: dict, enterprise: dict, values: dict,
                     stage: str, exec_id: int) -> None:
        task_id = task["id"]
        fp = task["fingerprint"]

        await driver.login(since_id=None)
        orch.save_checkpoint(task_id, {"stage": "logged_in"}, "logged_in")

        # ---- 幂等核对（第一道闸门，提交前必做）
        found = await self._reconcile_with_relogin(driver, fp)
        if found:
            await self._adopt_existing(driver, task, enterprise, found, exec_id)
            return

        # ---- 填表（会话中途过期时原地续跑，不放弃本次执行）
        # 重放一律走到第 3 步：只有提交按钮才在那一页，停在中间没有意义。
        try:
            await driver.open_declare(task["action"])
            res = await self._fill_steps(driver, task_id, values, stage)
        except SessionExpired:
            await driver.shot("mid_form_session_expired")
            await driver.login(since_id=driver.last_sms_id)
            # 重登后重放填写；重放不提交，因此不会重复申报
            res = await self._fill_steps(driver, task_id, values, stage, resumed=True)

        # ---- 提交前最后一次核对（第二道闸门）
        found = await self._reconcile_with_relogin(driver, fp)
        if found:
            await self._adopt_existing(driver, task, enterprise, found, exec_id)
            return

        # ---- 提交
        orch.mark_submitting(task_id, {"stage": "submitting", "draft_id": res.draft_id,
                                       "submitted_at": _now()})
        try:
            out = await driver.submit(values, res.draft_id)
        except SubmitOutcomeUnknown as e:
            await self._handle_unknown(driver, task, enterprise, values, str(e), exec_id)
            return

        await self._finalize_success(driver, task, enterprise, out.receipt_no,
                                     out.receipt_pdf, exec_id)

    async def _fill_steps(self, driver: SiteDriver, task_id: int, values: dict,
                          stage: str, resumed: bool = False) -> "StepResult":
        """填写并推进到「提交受理」页；每步落 checkpoint，供断点续跑。"""
        if stage == "start" and not resumed:
            res = await driver.fill_and_continue(values)
            orch.save_checkpoint(task_id, {"stage": "step2", "draft_id": res.draft_id}, "step2")
            res = await driver.confirm_step2(values, res.draft_id)
            orch.save_checkpoint(task_id, {"stage": "step3", "draft_id": res.draft_id}, "step3")
            return res
        # 续跑：从第 1 步重放（站点不保存跨会话草稿），重放不提交
        res = await driver.replay_to(values, "step3")
        orch.save_checkpoint(task_id, {"stage": "step3", "draft_id": res.draft_id}, "step3")
        return res

    # ------------------------------------------------------------ 核对
    async def _reconcile_with_relogin(self, driver: SiteDriver, fp: str) -> dict | None:
        for attempt in range(2):
            try:
                return await driver.query_submission(fp)
            except SessionExpired:
                if attempt:
                    raise
                await driver.login(since_id=driver.last_sms_id)
            except SiteUnavailable:
                raise
        return None

    async def _handle_unknown(self, driver: SiteDriver, task: dict, enterprise: dict,
                              values: dict, reason: str, exec_id: int) -> None:
        """提交结果未知：只读核对，查到就收下，查不到才允许重排。"""
        task_id = task["id"]
        orchestrator_meta = {"reason": reason}
        found = None
        for i in range(max(1, settings.reconcile_queries)):
            try:
                found = await self._reconcile_with_relogin(driver, task["fingerprint"])
            except (SessionExpired, SiteUnavailable):
                found = None
            if found:
                break
            await asyncio.sleep(settings.reconcile_delay_s)
        if found:
            await self._adopt_existing(driver, task, enterprise, found, exec_id,
                                       note="提交响应丢失，经站点核对确认已受理")
            return

        attempts = task["attempts"]
        max_attempts = settings.max_attempts
        if attempts >= max_attempts:
            orch.release_claim(task_id, "needs_fix", error_code="submit_unknown",
                               error_message="提交结果无法确认，请人工到站点核实后再重跑",
                               error_meta={"reason": reason, "reconcile_tried": settings.reconcile_queries})
            self.stats["needs_fix"] += 1
            orch.audit("engine", "submit_unknown_escalated", task_id, enterprise["id"], orchestrator_meta)
        else:
            orch.release_claim(task_id, "retry", error_code="submit_unknown",
                               error_message="提交结果未知且站点查无记录，已重新排队（会再次核对）",
                               error_meta={"reason": reason})
            orch.audit("engine", "submit_unknown_retry", task_id, enterprise["id"], orchestrator_meta)

    async def _adopt_existing(self, driver: SiteDriver, task: dict, enterprise: dict,
                              found: dict, exec_id: int, note: str = "站点核对发现该业务已受理") -> None:
        receipt_no = found["receipt_no"]
        pdf, shots = await driver.receipt_pdf_for(receipt_no)
        await self._finalize_success(driver, task, enterprise, receipt_no, pdf, exec_id,
                                     shots=shots, note=note, duplicated=True)

    async def _finalize_success(self, driver: SiteDriver, task: dict, enterprise: dict,
                                receipt_no: str, pdf: bytes | None, exec_id: int,
                                shots: list[str] | None = None, note: str | None = None,
                                duplicated: bool = False) -> None:
        task_id = task["id"]
        if not pdf:
            pdf, _ = await driver.receipt_pdf_for(receipt_no)
        shots = shots or driver.screenshots
        try:
            path, sha = receipt_mod.archive(task, enterprise, pdf, receipt_no, shots, exec_id)
        except Exception as e:  # 归档失败不能掩盖「已提交成功」
            path, sha = None, None
            orch.audit("engine", "receipt_archive_failed", task_id, enterprise["id"],
                       {"error": str(e), "receipt_no": receipt_no})
            handover = settings.receipts_dir / "PENDING" / f"{receipt_no}.pdf"
            handover.parent.mkdir(parents=True, exist_ok=True)
            if pdf:
                handover.write_bytes(pdf)

        checkpoint = {"stage": "done", "receipt_no": receipt_no}
        orch.mark_submitted(task_id, receipt_no, str(path) if path else None, checkpoint)
        self.stats["success"] += 1
        if duplicated:
            self.stats["duplicate"] += 1
        self._finish_execution(exec_id, "success", None,
                               {"receipt_no": receipt_no, "sha256": sha, "note": note,
                                "screenshots": [str(s) for s in shots]}, stage="done")
        orch.audit("engine", "task_success", task_id, enterprise["id"],
                   {"receipt_no": receipt_no, "sha256": sha, "note": note})

        # 回写花名册
        if task.get("batch_id"):
            from .writeback import update_roster_status
            try:
                update_roster_status(task["batch_id"], task["fingerprint"], "success",
                                     receipt_no=receipt_no, receipt_path=str(path) if path else None)
            except Exception as e:  # noqa: BLE001
                orch.audit("engine", "writeback_failed", task_id, enterprise["id"], {"error": str(e)})

    async def run_one_now(self, task_id: int) -> None:
        """控制台「立即执行/重跑」用。"""
        task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            return
        await self._execute(task_id)


def _quota_retry_at() -> str:
    """配额任务推迟到次日凌晨再试（调高配额后人工执行会立刻生效）。"""
    nxt = datetime.now() + timedelta(days=1)
    return nxt.replace(hour=1, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


class _StopExecution(Exception):
    def __init__(self, status: str, error_code: str):
        self.status = status
        self.error_code = error_code
