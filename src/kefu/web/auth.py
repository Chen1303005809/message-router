"""Enterprise-WeCom OAuth and short-lived server-side H5 sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import quote, unquote, urlencode
from urllib.request import urlopen
from uuid import uuid4

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from kefu.case_desk.contracts import Actor
from kefu.case_desk.errors import CaseDeskError
from kefu.config import Settings
from kefu.persistence.models import WebSession
from kefu.routing.directory import DatabaseRoutingDirectory

SESSION_COOKIE = "kefu_session"
OAUTH_STATE_COOKIE = "kefu_oauth_state"
SESSION_TTL = timedelta(hours=8)
OAUTH_STATE_TTL_SECONDS = 600


class AuthenticationError(CaseDeskError):
    pass


class OAuthClient(Protocol):
    def authorize_url(self, callback_url: str, state: str) -> str:
        """Build the enterprise-WeCom snsapi_base redirect URL."""

    def user_id_for_code(self, code: str) -> str:
        """Exchange a one-time OAuth code for a stable WeCom userid."""


class WeComOAuthClient:
    """Minimal official OAuth flow without exposing credentials to the browser."""

    def __init__(self, settings: Settings) -> None:
        if not settings.wecom_corp_id or not settings.wecom_corp_secret:
            raise AuthenticationError("企业微信 OAuth 尚未配置企业凭据")
        self._corp_id = settings.wecom_corp_id
        self._corp_secret = settings.wecom_corp_secret
        self._agent_id = settings.wecom_agent_id

    def authorize_url(self, callback_url: str, state: str) -> str:
        parameters = {
            "appid": self._corp_id,
            "redirect_uri": callback_url,
            "response_type": "code",
            "scope": "snsapi_base",
            "state": state,
        }
        if self._agent_id:
            parameters["agentid"] = self._agent_id
        return (
            "https://open.weixin.qq.com/connect/oauth2/authorize?"
            f"{urlencode(parameters)}#wechat_redirect"
        )

    def user_id_for_code(self, code: str) -> str:
        if not code or len(code) > 512:
            raise AuthenticationError("企业微信 OAuth 授权码无效")
        token_response = self._get_json(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            {"corpid": self._corp_id, "corpsecret": self._corp_secret},
        )
        token = token_response.get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthenticationError("无法获取企业微信访问凭据")
        user_response = self._get_json(
            "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo",
            {"access_token": token, "code": code},
        )
        user_id = user_response.get("userid")
        if not isinstance(user_id, str) or not user_id:
            raise AuthenticationError("当前企业微信身份不是可访问事件中心的企业成员")
        return user_id

    def _get_json(self, endpoint: str, parameters: Mapping[str, str]) -> Mapping[str, object]:
        query = urlencode(parameters)
        try:
            with urlopen(f"{endpoint}?{query}", timeout=5) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise AuthenticationError("企业微信身份验证暂时不可用") from error
        if not isinstance(payload, dict) or payload.get("errcode", 0) != 0:
            raise AuthenticationError("企业微信身份验证失败")
        return payload


class WebAuthenticator:
    """Translate a header or opaque server session into a CaseDesk actor."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        settings: Settings,
        oauth_client: OAuthClient | None = None,
        directory: DatabaseRoutingDirectory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._oauth_client = oauth_client
        self._directory = directory or DatabaseRoutingDirectory()

    def actor_for_request(self, request: Request) -> Actor | None:
        if self._settings.auth_mode == "development":
            wecom_userid = request.headers.get("X-WeCom-UserId") or self._settings.dev_wecom_userid
            if not wecom_userid:
                return None
            with self._session_factory() as session:
                user = self._directory.get_user_by_wecom_userid(session, wecom_userid)
                return Actor(user.id)
        if self._settings.auth_mode != "wecom_oauth":
            raise AuthenticationError("AUTH_MODE 必须为 development 或 wecom_oauth")
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            web_session = session.scalar(
                select(WebSession).where(WebSession.token_hash == token_hash).with_for_update()
            )
            if web_session is None or _as_utc(web_session.expires_at) <= now:
                return None
            user = self._directory.get_user(session, web_session.user_id)
            web_session.last_seen_at = now
            return Actor(user.id)

    def begin_login(self, next_path: str) -> RedirectResponse:
        client = self._oauth_client or WeComOAuthClient(self._settings)
        state = secrets.token_urlsafe(32)
        safe_next_path = _safe_next_path(
            next_path, mount_path=self._settings.web_mount_path
        )
        callback_url = f"{self._settings.web_base_url}/auth/callback"
        response = RedirectResponse(client.authorize_url(callback_url, state), status_code=302)
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            f"{state}|{quote(safe_next_path, safe='')}",
            max_age=OAUTH_STATE_TTL_SECONDS,
            httponly=True,
            secure=self._settings.session_cookie_secure,
            samesite="lax",
        )
        return response

    def complete_login(self, request: Request, *, code: str, state: str) -> RedirectResponse:
        raw_state = request.cookies.get(OAUTH_STATE_COOKIE)
        if raw_state is None:
            raise AuthenticationError("企业微信登录状态已过期，请重新打开事件中心")
        expected_state, separator, encoded_next_path = raw_state.partition("|")
        if not separator or not hmac.compare_digest(expected_state, state):
            raise AuthenticationError("企业微信登录状态校验失败")
        client = self._oauth_client or WeComOAuthClient(self._settings)
        wecom_userid = client.user_id_for_code(code)
        with self._session_factory() as session:
            user = self._directory.get_user_by_wecom_userid(session, wecom_userid)
        raw_token = secrets.token_urlsafe(48)
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            session.add(
                WebSession(
                    id=uuid4(),
                    token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                    user_id=user.id,
                    expires_at=now + SESSION_TTL,
                    created_at=now,
                    last_seen_at=now,
                )
            )
        response = RedirectResponse(
            _safe_next_path(
                unquote(encoded_next_path), mount_path=self._settings.web_mount_path
            ),
            status_code=302,
        )
        response.delete_cookie(OAUTH_STATE_COOKIE)
        response.set_cookie(
            SESSION_COOKIE,
            raw_token,
            max_age=int(SESSION_TTL.total_seconds()),
            httponly=True,
            secure=self._settings.session_cookie_secure,
            samesite="lax",
        )
        return response


def _safe_next_path(candidate: str, *, mount_path: str = "") -> str:
    """Keep OAuth returns under this public application mount point."""
    fallback = f"{mount_path}/events"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return fallback
    if mount_path and candidate != mount_path and not candidate.startswith(f"{mount_path}/"):
        return fallback
    return candidate


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
