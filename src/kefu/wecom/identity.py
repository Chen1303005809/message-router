"""Preserve the bot callback identity for persistent local resolution."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class WeComIdentityError(RuntimeError):
    """The callback did not contain a usable sender identity."""


class WeComUserIdResolver:
    """Normalize callback IDs without assuming WeCom can decrypt open_userid.

    In ``open_userid`` mode the callback value is kept intact. The routing
    directory resolves it through the member's persistent local binding.
    """

    def __init__(self, *, mode: str) -> None:
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"plain", "open_userid"}:
            raise ValueError("WECOM_BOT_USERID_MODE 必须为 plain 或 open_userid")
        self._mode = normalized_mode

    @property
    def is_open_userid_mode(self) -> bool:
        return self._mode == "open_userid"

    def resolve(self, callback_userid: str, *, msgid: str | None = None) -> str:
        """Return the callback ID unchanged for lookup by the routing directory."""
        normalized_userid = callback_userid.strip()
        if not normalized_userid:
            raise WeComIdentityError("企业微信回调缺少发送者身份")
        if self.is_open_userid_mode:
            logger.info(
                "WeCom callback open_userid retained for local binding lookup: "
                "msgid=%s open_userid=%s",
                msgid,
                normalized_userid,
            )
        else:
            logger.info(
                "WeCom callback userid passed through unchanged: msgid=%s userid=%s",
                msgid,
                normalized_userid,
            )
        return normalized_userid
