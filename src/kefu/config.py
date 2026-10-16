"""Process configuration with safe local defaults and explicit production knobs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    object_storage_backend: str
    object_storage_root: Path
    s3_endpoint_url: str | None
    s3_bucket: str
    s3_access_key_id: str | None
    s3_secret_access_key: str | None
    wecom_transport: str
    auth_mode: str
    web_base_url: str
    wecom_corp_id: str | None
    wecom_agent_id: str | None
    wecom_corp_secret: str | None
    dev_wecom_userid: str | None
    session_cookie_secure: bool
    wecom_bot_id: str | None = None
    wecom_bot_secret: str | None = None
    wecom_long_connection_url: str | None = None
    wecom_bot_mention_name: str | None = None
    wecom_connect_timeout_seconds: float = 20.0
    # Experimental only: hold a quoted developer-group callback in memory and
    # use it for a passive reply after the matching consultant-side message.
    wecom_deferred_passive_reply_enabled: bool = False
    wecom_deferred_passive_reply_ttl_seconds: float = 300.0
    # Optional first-run administrator bootstrap.  In production this can be
    # set before the web process starts; the H5 setup page provides a one-time
    # token flow when the identity is not known at deploy time.
    initial_admin_wecom_userid: str | None = None
    initial_admin_display_name: str = "问题工作台总管理员"
    admin_bootstrap_token: str | None = None
    agent_base_url: str | None = None
    agent_api_key: str | None = None
    agent_model: str | None = None
    issue_default_expiry_hours: int = 3
    issue_approaching_window_hours: int = 24
    issue_inspection_interval_hours: int = 12
    issue_inspection_scan_seconds: int = 60

    @property
    def web_mount_path(self) -> str:
        """Optional public path prefix embedded in ``WEB_BASE_URL``.

        The application routes remain rooted internally, while a reverse proxy
        can expose them below this prefix. For example,
        ``https://issues.example.com/kefu`` yields ``/kefu``.
        """
        path = urlsplit(self.web_base_url).path.rstrip("/")
        return path if path.startswith("/") else ""

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///./kefu.db"),
            object_storage_backend=os.getenv("OBJECT_STORAGE_BACKEND", "local"),
            object_storage_root=Path(os.getenv("OBJECT_STORAGE_ROOT", "./var/media")),
            s3_endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
            s3_bucket=os.getenv("S3_BUCKET", "kefu-media"),
            s3_access_key_id=os.getenv("S3_ACCESS_KEY_ID") or None,
            s3_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY") or None,
            wecom_transport=os.getenv("WECOM_TRANSPORT", "fake"),
            auth_mode=os.getenv("AUTH_MODE", "development"),
            web_base_url=os.getenv("WEB_BASE_URL", "http://localhost:8000").rstrip("/"),
            wecom_corp_id=os.getenv("WECOM_CORP_ID") or None,
            wecom_agent_id=os.getenv("WECOM_AGENT_ID") or None,
            wecom_corp_secret=os.getenv("WECOM_CORP_SECRET") or None,
            dev_wecom_userid=os.getenv("DEV_WECOM_USERID") or None,
            session_cookie_secure=os.getenv("SESSION_COOKIE_SECURE", "false").lower()
            in {"1", "true", "yes"},
            wecom_bot_id=os.getenv("WECOM_BOT_ID") or None,
            wecom_bot_secret=os.getenv("WECOM_BOT_SECRET") or None,
            wecom_long_connection_url=os.getenv("WECOM_LONG_CONNECTION_URL") or None,
            wecom_bot_mention_name=os.getenv("WECOM_BOT_MENTION_NAME") or None,
            wecom_connect_timeout_seconds=float(os.getenv("WECOM_CONNECT_TIMEOUT_SECONDS", "20")),
            wecom_deferred_passive_reply_enabled=os.getenv(
                "WECOM_DEFERRED_PASSIVE_REPLY_ENABLED", "false"
            ).lower()
            in {"1", "true", "yes"},
            wecom_deferred_passive_reply_ttl_seconds=float(
                os.getenv("WECOM_DEFERRED_PASSIVE_REPLY_TTL_SECONDS", "300")
            ),
            initial_admin_wecom_userid=os.getenv("INITIAL_ADMIN_WECOM_USERID") or None,
            initial_admin_display_name=os.getenv(
                "INITIAL_ADMIN_DISPLAY_NAME", "问题工作台总管理员"
            ),
            admin_bootstrap_token=os.getenv("ADMIN_BOOTSTRAP_TOKEN") or None,
            agent_base_url=os.getenv("AGENT_BASE_URL") or None,
            agent_api_key=os.getenv("AGENT_API_KEY") or None,
            agent_model=os.getenv("AGENT_MODEL") or None,
            issue_default_expiry_hours=_positive_int_env("ISSUE_DEFAULT_EXPIRY_HOURS", 3),
            issue_approaching_window_hours=_positive_int_env("ISSUE_APPROACHING_WINDOW_HOURS", 24),
            issue_inspection_interval_hours=_positive_int_env(
                "ISSUE_INSPECTION_INTERVAL_HOURS", 12
            ),
            issue_inspection_scan_seconds=_positive_int_env("ISSUE_INSPECTION_SCAN_SECONDS", 60),
        )


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} 必须是正整数") from error
    if value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value
