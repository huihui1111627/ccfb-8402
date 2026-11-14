"""运行配置。全部可用环境变量覆盖，便于测试隔离。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    @property
    def mock_base_url(self) -> str:
        return os.environ.get("MOCK_BASE_URL", "http://127.0.0.1:8100").rstrip("/")

    @property
    def artifacts_dir(self) -> Path:
        return Path(os.environ.get("RPA_ARTIFACTS", ROOT / "data" / "artifacts"))

    @property
    def receipts_dir(self) -> Path:
        return Path(os.environ.get("RPA_RECEIPTS", ROOT / "data" / "receipts"))

    @property
    def roster_in_dir(self) -> Path:
        return Path(os.environ.get("RPA_ROSTER_IN", self.data_dir / "roster_in"))

    @property
    def roster_out_dir(self) -> Path:
        return Path(os.environ.get("RPA_ROSTER_OUT", self.data_dir / "roster_out"))

    @property
    def data_dir(self) -> Path:
        return Path(os.environ.get("RPA_DATA_DIR", ROOT / "data"))

    @property
    def headless(self) -> bool:
        return _bool("RPA_HEADLESS", True)

    @property
    def browser_channel(self) -> str | None:
        return os.environ.get("RPA_BROWSER_CHANNEL") or None

    @property
    def max_attempts(self) -> int:
        return int(os.environ.get("RPA_MAX_ATTEMPTS", "3"))

    @property
    def step_timeout_ms(self) -> int:
        return int(os.environ.get("RPA_STEP_TIMEOUT_MS", "15000"))

    @property
    def sms_timeout_s(self) -> int:
        return int(os.environ.get("RPA_SMS_TIMEOUT_S", "30"))

    @property
    def nav_timeout_ms(self) -> int:
        return int(os.environ.get("RPA_NAV_TIMEOUT_MS", "20000"))

    @property
    def reconcile_queries(self) -> int:
        return int(os.environ.get("RPA_RECONCILE_QUERIES", "3"))

    @property
    def reconcile_delay_s(self) -> float:
        return float(os.environ.get("RPA_RECONCILE_DELAY_S", "1.0"))

    @property
    def backoff_base_s(self) -> float:
        """自动重试的退避基数。真实部署 60s 起，测试可调小。"""
        return float(os.environ.get("RPA_BACKOFF_BASE_S", "60"))

    @property
    def backoff_max_s(self) -> float:
        return float(os.environ.get("RPA_BACKOFF_MAX_S", "600"))

    @property
    def rate_limit_floor_s(self) -> float:
        return float(os.environ.get("RPA_RATE_LIMIT_FLOOR_S", "30"))


settings = Settings()
