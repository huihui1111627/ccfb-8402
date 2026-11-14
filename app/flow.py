"""Playwright 站点驱动。

设计要点
--------
1. **登录**：账号密码 → 短信验证码。取码前先记录网关当前最大短信 ID，
   只接受这之后到达的验证码，避免重复登录时误用旧码。
2. **会话过期续跑**：任何页面跳回登录页都抛 `SessionExpired`。上层重新登录后，
   依据已保存的表单值和草稿号重放到当前步骤；**提交前一定先做站点核对**，
   所以「重放」不会变成「重复提交」。
3. **提交结果未知**：`SubmitOutcomeUnknown`。调用方必须走核对接口，而不是直接重试。
4. **每步截图**：文件名带序号与阶段，便于人工复核。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .config import settings
from .sms import SmsClient


class SessionExpired(Exception):
    pass


class SubmitOutcomeUnknown(Exception):
    """提交动作已发出但没拿到确定结果：必须核对，不能重试。"""


class SiteUnavailable(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SiteValidationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class StepResult:
    stage: str
    draft_id: str | None = None
    receipt_no: str | None = None
    receipt_pdf: bytes | None = None
    screenshots: list[str] = field(default_factory=list)


class SiteDriver:
    def __init__(self, *, window: str, enterprise: dict, task: dict,
                 sms_client: SmsClient | None = None, artifacts_dir: Path | None = None,
                 headless: bool | None = None):
        self.window = window
        self.enterprise = enterprise
        self.task = task
        self.sms = sms_client or SmsClient()
        self.artifacts_dir = Path(artifacts_dir or settings.artifacts_dir)
        self.headless = settings.headless if headless is None else headless
        self.base = f"{settings.mock_base_url}/mock/{window}"
        self.account = enterprise[f"{window}_account"]
        self.password = enterprise[f"{window}_password"]
        self.phone = enterprise["phone"]

        self._pw = None
        self._browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.screenshots: list[str] = []
        self._step_no = 0
        self._login_count = 0
        self.last_sms_id: int = 0

    # ------------------------------------------------------------ 生命周期
    async def __aenter__(self) -> "SiteDriver":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self.context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        self.context.set_default_timeout(settings.step_timeout_ms)
        self.context.set_default_navigation_timeout(settings.nav_timeout_ms)
        self.page = await self.context.new_page()
        self.screenshots = []
        self._step_no = 0
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self.context:
                await self.context.close()
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # ------------------------------------------------------------ 工具
    async def shot(self, stage: str) -> str:
        self._step_no += 1
        d = self.artifacts_dir / f"task_{self.task['id']}" / f"attempt_{self.task.get('attempts', 1)}"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self._step_no:02d}_{stage}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True)
        except Exception:
            path.write_bytes(b"")
        self.screenshots.append(str(path))
        return str(path)

    async def _goto(self, url: str, stage: str) -> None:
        resp = await self.page.goto(url, wait_until="domcontentloaded")
        if resp and resp.status >= 500:
            await self.shot(f"{stage}_http500")
            raise SiteUnavailable(f"站点返回 {resp.status}", resp.status)
        if "/login" in self.page.url and "/declare" in url:
            await self.shot(f"{stage}_session_expired")
            raise SessionExpired("会话已过期")

    async def _click_and_nav(self, selector: str, stage: str) -> None:
        """点击按钮并等待表单 POST 完成。

        必须是「点击 + 等待导航」原子操作：站点常在 POST 后 303 回登录页，
        若只靠 wait_for_load_state，可能在旧页面上继续读取 DOM，把「已掉线」
        误判成「已进入下一步」，后面就会出现带着旧 draft_id 去提交。

        提交按钮（POST 后有重定向）用 expect_navigation；若站点是同步渲染
        （直接返回 200 表单页），navigation 可能不触发，此时回退到 load_state。
        """
        try:
            async with self.page.expect_navigation(wait_until="domcontentloaded",
                                                   timeout=settings.nav_timeout_ms):
                await self.page.click(selector)
        except Exception as e:  # noqa: BLE001
            await self.shot(f"{stage}_nav_timeout")
            raise SiteUnavailable(f"{stage} 页面跳转超时：{type(e).__name__}") from e
        if "/login" in self.page.url:
            await self.shot(f"{stage}_session_expired")
            raise SessionExpired(f"{stage} 后会话已过期")

    async def _expect_logged_in(self, stage: str) -> None:
        if "/login" in self.page.url:
            await self.shot(f"{stage}_session_expired")
            raise SessionExpired("会话已过期")

    async def error_text(self) -> tuple[str | None, str | None]:
        box = await self.page.query_selector("#error-box")
        if not box:
            return None, None
        text = (await box.inner_text()).strip()
        code = await box.get_attribute("data-error-code")
        return code, text

    # ------------------------------------------------------------ 登录
    async def login(self, since_id: int | None = None) -> None:
        """完整登录。since_id 用于只接受指定 ID 之后的验证码。"""
        self._login_count += 1
        await self._goto(f"{self.base}/login", "login_page")

        # 先记录「当前已存在的最新短信 id」，避免复用上一轮验证码
        if since_id is None:
            try:
                latest = await self.sms.latest(self.phone, "login")
                since_id = int(latest.get("id") or 0) if latest.get("found") else 0
            except Exception:
                since_id = 0

        await self.page.fill("#account", self.account)
        await self.page.fill("#password", self.password)
        await self.shot("login_credentials")
        await self.page.click("#btn-send")
        await self.page.wait_for_load_state("domcontentloaded")

        if await self.page.query_selector("#error-box"):
            code, text = await self.error_text()
            raise SiteValidationError(code or "auth_failed", text or "登录失败")

        await self.page.wait_for_selector("#code", timeout=settings.step_timeout_ms)
        await self.shot("login_sms_prompt")

        sms = await self.sms.wait_code(self.phone, "login", since_id=since_id,
                                       timeout_s=settings.sms_timeout_s)
        self.last_sms_id = sms.sms_id
        await self.page.fill("#code", sms.code)
        await self.shot("login_code_filled")
        await self.page.click("#btn-submit")
        await self.page.wait_for_load_state("domcontentloaded")

        if "/login" in self.page.url:
            code, text = await self.error_text()
            await self.shot("login_failed")
            raise SiteValidationError(code or "auth_failed", text or "短信验证码校验失败")
        await self.shot("login_ok")

    async def ensure_login(self) -> None:
        """轻量检查：直接打开首页，被重定向说明会话已失效。"""
        await self._goto(f"{self.base}/", "session_check")
        if "/login" in self.page.url:
            await self.login(since_id=self.last_sms_id)

    # ------------------------------------------------------------ 表单
    async def open_declare(self, action: str) -> None:
        await self._goto(f"{self.base}/declare?action={action}", "declare_open")
        await self._expect_logged_in("declare_open")
        if await self.page.query_selector("#error-box"):
            code, text = await self.error_text()
            raise SiteValidationError(code or "site_error", text or "站点拒绝打开申报页")

    async def _fill_step1(self, values: dict) -> None:
        p = self.page
        await p.fill("#employee_name", values["employee_name"])
        await p.fill("#id_card", values["id_card"])
        action = values["action"]
        if action != "add" and values.get("social_no"):
            if await p.query_selector("#social_no"):
                await p.fill("#social_no", values["social_no"])
        if action in ("add", "adjust") and values.get("base_after") is not None:
            await p.fill("#base_after", f"{float(values['base_after']):g}")
        await p.fill("#effective_month", values["effective_month"])
        if values.get("contact_phone"):
            await p.fill("#contact_phone", values["contact_phone"])

    async def fill_and_continue(self, values: dict) -> StepResult:
        """填写第 1 步并提交，处理服务端校验错误。"""
        await self._fill_step1(values)
        await self.shot("step1_filled")
        await self._click_and_nav("#btn-next", "step1")

        code, text = await self.error_text()
        if code:
            await self.shot("step1_error")
            raise SiteValidationError(code, text or "表单校验失败")

        # 第 2 步才有确认表；拿 draft_id
        draft_el = await self.page.query_selector('input[name="draft_id"]')
        draft_id = await draft_el.get_attribute("value") if draft_el else None
        if not draft_id:
            await self.shot("step1_no_draft")
            raise SiteValidationError("invalid_field", "未进入确认步骤（未拿到草稿号）")
        await self.shot("step2_confirm")
        return StepResult(stage="step2", draft_id=draft_id)

    async def confirm_step2(self, values: dict, draft_id: str) -> StepResult:
        await self._click_and_nav("#btn-next", "step2")
        code, text = await self.error_text()
        if code:
            await self.shot("step2_error")
            raise SiteValidationError(code, text or "确认信息失败")
        if not await self.page.query_selector("#btn-submit-final"):
            # 站点可能跳回第 1 步（校验失败但未给错误框）
            await self.shot("step2_unexpected")
            raise SiteValidationError("invalid_field", "未进入提交步骤")
        await self.shot("step3_ready")
        return StepResult(stage="step3", draft_id=draft_id)

    async def replay_to(self, values: dict, target_stage: str) -> StepResult:
        """会话过期后的续跑：从第 1 步重放至目标阶段，不产生提交。"""
        await self.open_declare(values["action"])
        r1 = await self.fill_and_continue(values)
        if target_stage in ("step2", "step2_filled"):
            return r1
        return await self.confirm_step2(values, r1.draft_id)

    # ------------------------------------------------------------ 提交
    async def submit(self, values: dict, draft_id: str) -> StepResult:
        """点击最终提交。

        调用前调用方必须已把任务标为 submitting 并完成站点核对。
        """
        # 前置守卫：确保当前就在「提交受理」页，否则绝不点提交
        if not await self.page.query_selector("#btn-submit-final"):
            await self.shot("submit_wrong_stage")
            raise SiteValidationError("invalid_field", "当前不在提交步骤，已中止提交")
        # 提交前最后一次本地守卫：页面上的证件号/生效月份必须与任务一致，
        # 避免把上一个人的表单当成这个人提交。
        try:
            body = await self.page.inner_text("body")
        except Exception:
            body = ""
        if values["id_card"] not in body:
            await self.shot("submit_precheck_failed")
            raise SiteValidationError("invalid_field", "提交页证件号与任务不一致，已中止提交")
        if values["effective_month"] not in body:
            await self.shot("submit_precheck_failed")
            raise SiteValidationError("invalid_field", "提交页生效月份与任务不一致，已中止提交")

        await self.shot("submit_before_click")
        try:
            async with self.page.expect_navigation(wait_until="domcontentloaded",
                                                   timeout=settings.nav_timeout_ms):
                await self.page.click("#btn-submit-final")
        except Exception as e:  # 超时/断连：结果未知
            await self.shot("submit_response_lost")
            raise SubmitOutcomeUnknown(f"提交后未收到响应：{type(e).__name__}") from e

        if "/login" in self.page.url:
            # 关键：提交点击已经发生，会话过期不代表没提交。必须按「结果未知」处理，
            # 由上层用站点查询接口核对，绝不能直接重试。
            await self.shot("submit_session_expired_after_click")
            raise SubmitOutcomeUnknown("提交后会话过期，结果未知，需核对")
        if await self.page.query_selector("#error-box"):
            code, text = await self.error_text()
            await self.shot("submit_error")
            raise SiteValidationError(code or "site_error", text or "提交被拒绝")

        receipt = await self.page.query_selector("#receipt-no")
        if not receipt:
            await self.shot("submit_unknown_page")
            raise SubmitOutcomeUnknown("提交后未出现回执页，结果未知")

        receipt_no = (await receipt.inner_text()).strip()
        pdf = await self.page.pdf(format="A4", print_background=True)
        await self.shot("receipt")
        return StepResult(stage="done", draft_id=draft_id, receipt_no=receipt_no, receipt_pdf=pdf)

    # ------------------------------------------------------------ 核对
    async def query_submission(self, fingerprint: str, receipt_no: str = "") -> dict | None:
        """用站点查询接口核对是否已受理。

        通过浏览器上下文发请求，天然带上登录 Cookie；会话失效时抛出
        SessionExpired，由上层重新登录后重试核对（核对是只读的，安全）。
        """
        params = {"window": self.window}
        if fingerprint:
            params["fingerprint"] = fingerprint
        if receipt_no:
            params["receipt_no"] = receipt_no
        resp = await self.context.request.get(f"{self.base}/query", params=params)
        if resp.status == 401:
            await self.shot("query_session_expired")
            raise SessionExpired("核对时会话过期")
        if resp.status >= 500:
            raise SiteUnavailable(f"核对接口 {resp.status}", resp.status)
        if resp.status != 200:
            raise SiteValidationError("site_error", f"核对接口返回 {resp.status}")
        data = await resp.json()
        return data if data.get("found") else None

    async def receipt_pdf_for(self, receipt_no: str) -> tuple[bytes, list[str]]:
        await self._goto(f"{self.base}/receipt/{receipt_no}", "receipt_fetch")
        await self._expect_logged_in("receipt_fetch")
        await self.shot("receipt_recovered")
        return await self.page.pdf(format="A4", print_background=True), list(self.screenshots)
