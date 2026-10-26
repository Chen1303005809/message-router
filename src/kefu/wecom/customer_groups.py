"""Small server-side client for customer-group context and WeCom JS-SDK setup."""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any

import httpx

from kefu.config import Settings
from kefu.tools import LoggedHttpClient

_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
logger = logging.getLogger(__name__)


class WeComCustomerGroupError(RuntimeError):
    """A customer-group or JS-SDK lookup could not be completed safely."""


class WeComCustomerGroupClient:
    """Look up customer groups with the configured self-built application secret.

    The secret must be added to the enterprise's callable customer-contact applications.
    Access tokens and the agent JS-SDK ticket are cached in this process for their
    short-lived validity windows.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        http: LoggedHttpClient | None = None,
    ) -> None:
        self._corp_id = settings.wecom_corp_id
        self._corp_secret = settings.wecom_corp_secret
        self._agent_id = settings.wecom_agent_id
        self._http = http or LoggedHttpClient(timeout=8)
        self._cache_lock = threading.Lock()
        self._access_token: tuple[str, float] | None = None
        self._agent_ticket: tuple[str, float] | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._corp_id and self._corp_secret and self._agent_id)

    def customer_group_name_for_member(self, chat_id: str, wecom_userid: str) -> str:
        """Return a verified customer-group name for a current internal member."""
        normalized_chat_id = chat_id.strip() if isinstance(chat_id, str) else ""
        normalized_userid = wecom_userid.strip() if isinstance(wecom_userid, str) else ""
        if not normalized_chat_id or len(normalized_chat_id) > 256:
            raise WeComCustomerGroupError("客户群标识无效")
        if not normalized_userid:
            raise WeComCustomerGroupError("企业微信成员身份缺失")

        payload = self._post_json(
            "/externalcontact/groupchat/get",
            {"chat_id": normalized_chat_id},
            operation="wecom_customer_group_get",
        )
        group_chat = payload.get("group_chat")
        if not isinstance(group_chat, Mapping):
            raise WeComCustomerGroupError("企业微信未返回客户群详情")
        if group_chat.get("chat_id") != normalized_chat_id:
            raise WeComCustomerGroupError("企业微信返回了不匹配的客户群")

        member_list = group_chat.get("member_list")
        is_member = isinstance(member_list, list) and any(
            isinstance(member, Mapping)
            and member.get("type") == 1
            and member.get("userid") == normalized_userid
            for member in member_list
        )
        if not is_member:
            raise WeComCustomerGroupError("当前成员不属于该客户群")

        name = group_chat.get("name")
        if not isinstance(name, str) or not name.strip():
            raise WeComCustomerGroupError("客户群名称为空")
        return name.strip()

    def agent_js_sdk_config(self, page_url: str) -> dict[str, str | int]:
        """Build the signed app-identity configuration for wx.agentConfig."""
        if not self.is_configured:
            raise WeComCustomerGroupError("企业微信 JS-SDK 尚未配置")
        assert self._corp_id is not None
        assert self._agent_id is not None
        try:
            agent_id = int(self._agent_id)
        except ValueError as error:
            raise WeComCustomerGroupError("企业微信 Agent ID 无效") from error

        ticket = self._get_agent_ticket()
        timestamp = int(time.time())
        nonce_str = secrets.token_urlsafe(18)
        signature = hashlib.sha1(
            (
                f"jsapi_ticket={ticket}&noncestr={nonce_str}&timestamp={timestamp}"
                f"&url={page_url}"
            ).encode()
        ).hexdigest()
        return {
            "corpid": self._corp_id,
            "agentid": agent_id,
            "timestamp": timestamp,
            "nonceStr": nonce_str,
            "signature": signature,
        }

    def _get_access_token(self) -> str:
        if not self._corp_id or not self._corp_secret:
            raise WeComCustomerGroupError("企业微信客户联系凭证尚未配置")
        now = time.monotonic()
        with self._cache_lock:
            if self._access_token is not None and self._access_token[1] > now:
                return self._access_token[0]
            payload = self._request_json(
                "GET",
                "/gettoken",
                operation="wecom_customer_group_gettoken",
                params={"corpid": self._corp_id, "corpsecret": self._corp_secret},
            )
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise WeComCustomerGroupError("企业微信未返回访问凭据")
            expires_in = _positive_seconds(payload.get("expires_in"), default=7200)
            self._access_token = (token, now + max(1, expires_in - 60))
            return token

    def _get_agent_ticket(self) -> str:
        now = time.monotonic()
        with self._cache_lock:
            if self._agent_ticket is not None and self._agent_ticket[1] > now:
                return self._agent_ticket[0]
            token = self._get_access_token_locked()
            payload = self._request_json(
                "GET",
                "/ticket/get",
                operation="wecom_agent_jsapi_ticket",
                params={"access_token": token, "type": "agent_config"},
            )
            ticket = payload.get("ticket")
            if not isinstance(ticket, str) or not ticket:
                raise WeComCustomerGroupError("企业微信未返回 JS-SDK 凭据")
            expires_in = _positive_seconds(payload.get("expires_in"), default=7200)
            self._agent_ticket = (ticket, now + max(1, expires_in - 60))
            return ticket

    def _get_access_token_locked(self) -> str:
        """Get the token while the reentrant cache lock is already held."""
        if not self._corp_id or not self._corp_secret:
            raise WeComCustomerGroupError("企业微信客户联系凭证尚未配置")
        now = time.monotonic()
        if self._access_token is not None and self._access_token[1] > now:
            return self._access_token[0]
        payload = self._request_json(
            "GET",
            "/gettoken",
            operation="wecom_customer_group_gettoken",
            params={"corpid": self._corp_id, "corpsecret": self._corp_secret},
        )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise WeComCustomerGroupError("企业微信未返回访问凭据")
        expires_in = _positive_seconds(payload.get("expires_in"), default=7200)
        self._access_token = (token, now + max(1, expires_in - 60))
        return token

    def _post_json(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        operation: str,
    ) -> Mapping[str, Any]:
        return self._request_json(
            "POST",
            path,
            operation=operation,
            params={"access_token": self._get_access_token()},
            json=body,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        try:
            response = self._http.request(
                method,
                f"{_API_BASE}{path}",
                operation=operation,
                **kwargs,
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise WeComCustomerGroupError("企业微信客户群服务暂时不可用") from error
        if not isinstance(payload, dict):
            raise WeComCustomerGroupError("企业微信返回了无效响应")
        errcode = payload.get("errcode")
        if errcode != 0:
            if isinstance(errcode, int) and not isinstance(errcode, bool):
                diagnostic_errcode = str(errcode)
            elif isinstance(errcode, str) and len(errcode) <= 32 and all(
                char.isascii() and (char.isalnum() or char in "_.:-") for char in errcode
            ):
                diagnostic_errcode = errcode or "unavailable"
            else:
                diagnostic_errcode = "unavailable"
            logger.warning(
                "wecom_customer_group_api_error operation=%s errcode=%s",
                operation,
                diagnostic_errcode,
            )
            raise WeComCustomerGroupError("企业微信客户群查询未获授权或失败")
        return payload


def _positive_seconds(value: object, *, default: int) -> int:
    try:
        seconds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return seconds if seconds > 0 else default
