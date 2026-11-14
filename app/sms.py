"""短信验证码取码客户端。

设计上把「取码」与「站点登录」解耦：
- 默认从 mock 短信网关 HTTP 接口取
- 真实环境替换为运营商短信 API / 人工输入队列，接口一致
- 取码必须携带 since_id，只认本次登录请求之后到达的短信，避免错用上一条
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from .config import settings


class SmsTimeout(Exception):
    pass


@dataclass
class SmsCode:
    code: str
    sms_id: int
    phone: str
    body: str


class SmsClient:
    def __init__(self, base_url: str | None = None, timeout_s: float | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = (base_url or settings.mock_base_url).rstrip("/")
        self.timeout_s = timeout_s or settings.sms_timeout_s
        self.transport = transport

    async def latest(self, phone: str, purpose: str = "login", since_id: int = 0) -> dict:
        params = {"phone": phone, "purpose": purpose}
        if since_id:
            params["since_id"] = since_id
        async with httpx.AsyncClient(transport=self.transport, timeout=10) as client:
            r = await client.get(f"{self.base_url}/mock/sms/api/latest", params=params)
            r.raise_for_status()
            return r.json()

    async def wait_code(self, phone: str, purpose: str = "login", since_id: int = 0,
                        timeout_s: float | None = None, poll_s: float = 1.0,
                        on_wait: Callable[[], None] | None = None) -> SmsCode:
        deadline = time.time() + (timeout_s or self.timeout_s)
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                data = await self.latest(phone, purpose, since_id)
                if data.get("found"):
                    return SmsCode(code=str(data["code"]), sms_id=int(data["id"]),
                                   phone=data.get("phone", phone), body=data.get("body", ""))
            except Exception as e:  # noqa: BLE001
                last_err = e
            if on_wait:
                on_wait()
            await asyncio.sleep(poll_s)
        raise SmsTimeout(f"{self.timeout_s}s 内未收到短信验证码"
                         + (f"（最后错误：{last_err}）" if last_err else ""))

    def code_from_body(self, body: str) -> str | None:
        import re
        m = re.search(r"(\d{6})", body)
        return m.group(1) if m else None
