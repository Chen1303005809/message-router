"""Enterprise-WeCom member identity conversion for intelligent-bot callbacks."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class WeComIdentityError(RuntimeError):
    """The callback identity could not be resolved to a canonical WeCom userid."""


class WeComUserIdResolver:
    """Resolve bot callback IDs to the plain member userid used by the event desk.

    Bots created by a non-superadmin may return an encrypted open_userid. The
    self-built app credentials are used to convert that ID through WeCom's
    openuserid_to_userid API before it enters the business layer.
    """

    _TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    _CONVERT_URL = "https://qyapi.weixin.qq.com/cgi-bin/batch/openuserid_to_userid"
    _HTTP_TIMEOUT_SECONDS = 5

    def __init__(
        self,
        *,
        mode: str,
        corp_id: str | None = None,
        corp_secret: str | None = None,
        source_agent_id: str | None = None,
    ) -> None:
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"plain", "open_userid"}:
            raise ValueError("WECOM_BOT_USERID_MODE 必须为 plain 或 open_userid")
        self._mode = normalized_mode
        self._corp_id = (corp_id or "").strip()
        self._corp_secret = (corp_secret or "").strip()
        self._source_agent_id = (source_agent_id or "").strip()
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._resolved_userids: dict[str, str] = {}

        if self._mode == "open_userid":
            if not self._corp_id or not self._corp_secret or not self._source_agent_id:
                raise ValueError(
                    "WECOM_BOT_USERID_MODE=open_userid 需要配置 "
                    "WECOM_CORP_ID、WECOM_CORP_SECRET 和 WECOM_AGENT_ID"
                )
            try:
                int(self._source_agent_id)
            except ValueError as error:
                raise ValueError("WECOM_AGENT_ID 必须是数字") from error

    def resolve(self, callback_userid: str) -> str:
        """Return a plain userid, failing closed when encrypted-ID conversion fails."""
        normalized_userid = callback_userid.strip()
        if not normalized_userid:
            raise WeComIdentityError("企业微信回调缺少发送者身份")
        if self._mode == "plain":
            return normalized_userid

        with self._lock:
            cached = self._resolved_userids.get(normalized_userid)
            if cached is not None:
                return cached

            token = self._get_access_token()
            response = self._post_json(
                f"{self._CONVERT_URL}?{urlencode({'access_token': token})}",
                {
                    "open_userid_list": [normalized_userid],
                    "source_agentid": int(self._source_agent_id),
                },
            )
            userid = self._plain_userid_from_response(response, normalized_userid)
            self._resolved_userids[normalized_userid] = userid
            return userid

    def _get_access_token(self) -> str:
        now = time.monotonic()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        response = self._get_json(
            self._TOKEN_URL,
            {"corpid": self._corp_id, "corpsecret": self._corp_secret},
        )
        token = response.get("access_token")
        if not isinstance(token, str) or not token:
            raise WeComIdentityError("无法获取企业微信身份转换凭据")
        expires_in = response.get("expires_in", 7200)
        if not isinstance(expires_in, int) or expires_in <= 0:
            expires_in = 7200
        self._access_token = token
        self._access_token_expires_at = now + max(1, expires_in - 60)
        return token

    def _plain_userid_from_response(
        self, response: Mapping[str, object], requested_open_userid: str
    ) -> str:
        converted = response.get("userid_list")
        if isinstance(converted, list):
            for item in converted:
                if not isinstance(item, Mapping):
                    continue
                open_userid = item.get("open_userid")
                userid = item.get("userid")
                if open_userid == requested_open_userid and isinstance(userid, str) and userid:
                    return userid
        raise WeComIdentityError(
            "企业微信未能转换发送者身份，请检查自建应用与智能机器人对接及成员可见范围"
        )

    @classmethod
    def _get_json(cls, endpoint: str, parameters: Mapping[str, str]) -> Mapping[str, object]:
        try:
            with urlopen(  # noqa: S310
                f"{endpoint}?{urlencode(parameters)}",
                timeout=cls._HTTP_TIMEOUT_SECONDS,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise WeComIdentityError("企业微信身份转换服务暂时不可用") from error
        if not isinstance(payload, dict) or payload.get("errcode", 0) != 0:
            raise WeComIdentityError("企业微信身份转换凭据无效")
        return payload

    @classmethod
    def _post_json(
        cls, endpoint: str, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=cls._HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
                result = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise WeComIdentityError("企业微信身份转换服务暂时不可用") from error
        if not isinstance(result, dict) or result.get("errcode", 0) != 0:
            raise WeComIdentityError("企业微信无法转换该发送者身份")
        return result
