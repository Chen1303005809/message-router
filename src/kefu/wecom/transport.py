"""Transport boundary between the event desk and WeCom intelligent bots.

Only this module understands callback frames, media URLs and active-push
protocol details. The relay receives normalized content parts and the durable
delivery worker sends persisted text/image items through the same interface.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from kefu.media.storage import ObjectStorage
from kefu.persistence.models import DeliveryDestination, MessageIntent, PartKind, StoredMedia
from kefu.wecom.aibot_client import AiBotClient, AiBotClientError, OfficialAiBotClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InboundTextPart:
    text: str


@dataclass(frozen=True, slots=True)
class InboundImagePart:
    """A decrypted image ready for immediate internal storage."""

    data: bytes
    mime_type: str


type InboundPart = InboundTextPart | InboundImagePart


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """Normalized inbound envelope with no platform JSON in business code."""

    msgid: str
    sender_userid: str
    chatid: str | None
    chattype: str
    parts: tuple[InboundPart, ...]
    quote_content: str | None = None
    mentioned_bot: bool = False
    intent: MessageIntent = MessageIntent.HANDOFF
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    destination_type: DeliveryDestination
    destination_address: str
    req_id: str
    kind: PartKind
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    req_id: str
    platform_result: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReplyAction:
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class InboundReply:
    """An immediate response to the inbound callback that prompted it."""

    text: str
    title: str = "客户问题事件中心"
    actions: tuple[ReplyAction, ...] = ()


class WeComTransport(Protocol):
    async def start(self) -> None:
        """Connect and wait until the transport is ready to send."""

    async def close(self) -> None:
        """Release a long connection during worker shutdown."""

    def events(self) -> AsyncIterator[InboundEvent]:
        """Yield normalized inbound callbacks from a long-lived connection."""

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """Push one already-persisted item to a user or group conversation."""

    async def reply(self, event: InboundEvent, reply: InboundReply) -> DeliveryReceipt:
        """Reply to the original callback without creating a durable delivery."""


class TransportError(RuntimeError):
    pass


class InboundPayloadError(TransportError):
    pass


class FakeWeComAdapter:
    """Deterministic test transport that records calls and can fail by req_id."""

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self.replies: list[tuple[InboundEvent, InboundReply]] = []
        self._inbound: list[InboundEvent] = []
        self._remaining_failures: dict[str, int] = defaultdict(int)

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def push_inbound(self, event: InboundEvent) -> None:
        self._inbound.append(event)

    def fail_next(self, req_id: str, *, times: int = 1) -> None:
        if times < 1:
            raise ValueError("times must be positive")
        self._remaining_failures[req_id] += times

    async def events(self) -> AsyncIterator[InboundEvent]:
        while self._inbound:
            yield self._inbound.pop(0)

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        if self._remaining_failures[message.req_id] > 0:
            self._remaining_failures[message.req_id] -= 1
            raise TransportError(f"planned failure for {message.req_id}")
        self.sent.append(message)
        return DeliveryReceipt(req_id=message.req_id, platform_result={"fake": "accepted"})

    async def reply(self, event: InboundEvent, reply: InboundReply) -> DeliveryReceipt:
        self.replies.append((event, reply))
        return DeliveryReceipt(req_id=f"reply-{event.msgid}", platform_result={"fake": "accepted"})


class LongConnectionWeComAdapter:
    """Real intelligent-bot WebSocket adapter.

    The adapter uses WeCom's Python SDK for WebSocket lifecycle and encrypted
    downloads. It converts both ``text`` and ordered ``mixed.msg_item``
    callbacks before they reach the relay, uploads internal images as temporary
    WeCom media, and preserves a delivery item's persisted ``req_id`` on active
    sends.
    """

    def __init__(
        self,
        *,
        bot_id: str,
        secret: str,
        session_factory: Callable[[], Session],
        storage: ObjectStorage,
        ws_url: str | None = None,
        bot_mention_name: str | None = None,
        require_group_mention: bool = True,
        connect_timeout_seconds: float = 20,
        client: AiBotClient | None = None,
    ) -> None:
        self._bot_id = _nonempty(bot_id, "WECOM_BOT_ID")
        self._secret = _nonempty(secret, "WECOM_BOT_SECRET")
        if connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        self._session_factory = session_factory
        self._storage = storage
        self._bot_mention_name = bot_mention_name.strip() if bot_mention_name else None
        self._require_group_mention = require_group_mention
        self._connect_timeout_seconds = connect_timeout_seconds
        self._client = client or OfficialAiBotClient(
            bot_id=self._bot_id,
            secret=self._secret,
            ws_url=ws_url,
        )
        self._events: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._last_connection_error: Exception | None = None

    async def start(self) -> None:
        """Start one authenticated connection; a bot may only have one active one."""
        async with self._start_lock:
            if self._closed:
                raise TransportError("企业微信长连接已经关闭")
            if self._started:
                return
            self._client.on("message", self._on_raw_message)
            self._client.on("authenticated", self._on_authenticated)
            self._client.on("disconnected", self._on_disconnected)
            self._client.on("error", self._on_connection_error)
            self._started = True
            connect_task = asyncio.create_task(self._client.connect())
            ready_task = asyncio.create_task(self._ready.wait())
            deadline = asyncio.get_running_loop().time() + self._connect_timeout_seconds
            try:
                done, _ = await asyncio.wait(
                    (connect_task, ready_task),
                    timeout=self._connect_timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if ready_task in done:
                    return
                if not done:
                    raise TimeoutError
                connect_task.result()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.wait_for(ready_task, timeout=remaining)
            except TimeoutError as error:
                self._started = False
                self._client.disconnect()
                detail = str(self._last_connection_error or "认证未在限定时间内完成")
                raise TransportError(f"企业微信长连接认证超时：{detail}") from error
            except Exception as error:
                self._started = False
                self._client.disconnect()
                raise TransportError("企业微信长连接启动失败") from error
            finally:
                for task in (connect_task, ready_task):
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass

    async def close(self) -> None:
        self._closed = True
        self._ready.clear()
        if self._started:
            self._client.disconnect()
        self._started = False

    async def events(self) -> AsyncIterator[InboundEvent]:
        await self.start()
        while not self._closed:
            yield await self._events.get()

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        if not self._started:
            await self.start()
        if not self._ready.is_set() or not self._client.is_connected:
            raise TransportError("企业微信长连接当前不可用，投递会自动重试")
        if not message.destination_address.strip():
            raise TransportError("企业微信投递目标为空")
        try:
            if message.kind is PartKind.TEXT:
                raw_card = message.payload.get("template_card")
                if raw_card is not None:
                    result = await self._client.send_template_card(
                        chatid=message.destination_address,
                        req_id=message.req_id,
                        card=_mapping(raw_card, "企业微信模板卡片内容无效"),
                    )
                else:
                    content = _payload_string(message.payload, "content")
                    result = await self._client.send_markdown(
                        chatid=message.destination_address,
                        req_id=message.req_id,
                        content=content,
                    )
            elif message.kind is PartKind.IMAGE:
                source_data, filename = self._load_stored_image(message.payload)
                wecom_media_id = await self._client.upload_image(
                    data=source_data,
                    filename=filename,
                )
                result = await self._client.send_image(
                    chatid=message.destination_address,
                    req_id=message.req_id,
                    media_id=wecom_media_id,
                )
            else:  # pragma: no cover - guarded by database constraint, kept safe at the seam.
                raise TransportError(f"不支持的企业微信投递类型：{message.kind}")
        except (AiBotClientError, InboundPayloadError, OSError, ValueError) as error:
            raise TransportError(str(error)) from error
        except Exception as error:
            raise TransportError("企业微信主动推送失败") from error
        return DeliveryReceipt(req_id=message.req_id, platform_result=_platform_result(result))

    async def reply(self, event: InboundEvent, reply: InboundReply) -> DeliveryReceipt:
        # Keep the original callback envelope intact.  WeCom binds a passive
        # response to the outer callback headers/req_id; body.quote is merely
        # the message content that the user quoted inside the inbound event.
        raw_frame = event.metadata.get("wecom_raw_frame")
        if not isinstance(raw_frame, Mapping):
            raise TransportError("入站消息缺少企业微信回调上下文，无法回复")
        try:
            if reply.actions:
                result = await self._client.reply_card(
                    frame=cast(Mapping[str, object], raw_frame),
                    card=_reply_card(reply),
                )
            else:
                result = await self._client.reply_markdown(
                    frame=cast(Mapping[str, object], raw_frame),
                    content=reply.text,
                )
        except Exception as error:
            raise TransportError("企业微信被动回复失败") from error
        return DeliveryReceipt(
            req_id=f"reply-{event.msgid}", platform_result=_platform_result(result)
        )

    def _on_authenticated(self, *_: object) -> None:
        self._last_connection_error = None
        self._ready.set()
        logger.info("WeCom long connection authenticated")

    def _on_disconnected(self, reason: object = "", *_: object) -> None:
        self._ready.clear()
        logger.warning("WeCom long connection disconnected: %s", reason)

    def _on_connection_error(self, error: object = None, *_: object) -> None:
        self._last_connection_error = (
            error if isinstance(error, Exception) else TransportError(str(error))
        )
        self._ready.clear()
        logger.warning("WeCom long connection error: %s", error)

    def _on_raw_message(self, frame: object, *_: object) -> None:
        if not isinstance(frame, Mapping):
            logger.warning("ignored non-object WeCom callback")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.exception("WeCom callback arrived outside the worker event loop")
            return
        loop.create_task(self._normalize_and_queue(cast(Mapping[str, object], frame)))

    async def _normalize_and_queue(self, frame: Mapping[str, object]) -> None:
        try:
            event = await self._normalize_frame(frame)
        except InboundPayloadError as error:
            event = self._error_event(frame, str(error))
        except Exception:
            logger.exception("failed to normalize WeCom callback")
            event = self._error_event(frame, "消息内容解析失败，请重新发送。")
        await self._events.put(event)

    async def _normalize_frame(self, frame: Mapping[str, object]) -> InboundEvent:
        body = _mapping(frame.get("body"), "企业微信回调缺少消息体")
        msgid = _required_callback_string(body, "msgid")
        sender = _mapping(body.get("from"), "企业微信回调缺少发送人")
        sender_userid = _required_callback_string(sender, "userid")
        chattype = _required_callback_string(body, "chattype")
        if chattype not in {"single", "group"}:
            raise InboundPayloadError("消息会话类型不受支持")
        chatid = body.get("chatid")
        if chatid is not None and not isinstance(chatid, str):
            raise InboundPayloadError("企业微信群聊标识无效")
        if chattype == "group" and not chatid:
            raise InboundPayloadError("企业微信群聊消息缺少 chatid")
        parts = await self._parts_from_body(body)
        raw_frame = _copy_frame(frame)
        return InboundEvent(
            msgid=msgid,
            sender_userid=sender_userid,
            chatid=chatid,
            chattype=chattype,
            parts=parts,
            quote_content=_quote_text(body.get("quote")),
            mentioned_bot=self._is_bot_mentioned(body, parts, chattype),
            metadata={"wecom_raw_frame": raw_frame},
        )

    async def _parts_from_body(self, body: Mapping[str, object]) -> tuple[InboundPart, ...]:
        msgtype = _required_callback_string(body, "msgtype")
        if msgtype == "text":
            return (InboundTextPart(_text_content(body.get("text"))),)
        if msgtype == "image":
            return (await self._image_part(_mapping(body.get("image"), "图片消息缺少图片内容")),)
        if msgtype != "mixed":
            raise InboundPayloadError("MVP 当前只支持文字和图文混排消息")
        mixed = _mapping(body.get("mixed"), "图文混排消息缺少 mixed 内容")
        items = mixed.get("msg_item")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or not items:
            raise InboundPayloadError("图文混排消息不包含内容片段")
        parts: list[InboundPart] = []
        for item in items:
            item_map = _mapping(item, "图文混排含有无效内容片段")
            item_type = _required_callback_string(item_map, "msgtype")
            if item_type == "text":
                parts.append(InboundTextPart(_text_content(item_map.get("text"))))
            elif item_type == "image":
                parts.append(
                    await self._image_part(
                        _mapping(item_map.get("image"), "图文混排图片缺少图片内容")
                    )
                )
            else:
                raise InboundPayloadError("图文混排只支持文字和图片片段")
        return tuple(parts)

    async def _image_part(self, image: Mapping[str, object]) -> InboundImagePart:
        url = _required_callback_string(image, "url")
        aes_key = image.get("aeskey")
        if aes_key is not None and not isinstance(aes_key, str):
            raise InboundPayloadError("图片解密密钥无效")
        try:
            data, filename = await self._client.download_file(url, aes_key)
        except Exception as error:
            raise InboundPayloadError("图片下载或解密失败，请重新发送。") from error
        mime_type = _image_mime_type(data, filename, url, image.get("mime_type"))
        return InboundImagePart(data=data, mime_type=mime_type)

    def _is_bot_mentioned(
        self, body: Mapping[str, object], parts: Sequence[InboundPart], chattype: str
    ) -> bool:
        if chattype != "group":
            return False
        if not self._require_group_mention:
            return True
        for mention_field in ("mentioned_bot", "is_at", "is_mentioned", "at_bot"):
            if body.get(mention_field) is True:
                return True
        for mention_list_field in (
            "at_list",
            "at_userids",
            "at_users",
            "mention_list",
            "mentioned_list",
            "mentioned_userids",
        ):
            if _contains_identifier(body.get(mention_list_field), self._bot_id):
                return True
        if self._bot_mention_name:
            normalized_name = self._bot_mention_name.lstrip("@＠")
            tokens = (f"@{normalized_name}", f"＠{normalized_name}")
            for part in parts:
                if isinstance(part, InboundTextPart) and any(
                    token in part.text for token in tokens
                ):
                    return True
        return False

    def _error_event(self, frame: Mapping[str, object], error_message: str) -> InboundEvent:
        body = frame.get("body")
        body_map = body if isinstance(body, Mapping) else {}
        sender = body_map.get("from")
        sender_map = sender if isinstance(sender, Mapping) else {}
        msgid = body_map.get("msgid")
        safe_msgid = (
            msgid.strip() if isinstance(msgid, str) and msgid.strip() else f"invalid-{uuid4()}"
        )
        return InboundEvent(
            msgid=safe_msgid,
            sender_userid=str(sender_map.get("userid") or ""),
            chatid=body_map.get("chatid") if isinstance(body_map.get("chatid"), str) else None,
            chattype=str(body_map.get("chattype") or "single"),
            parts=(),
            metadata={"wecom_raw_frame": _copy_frame(frame), "decode_error": error_message},
        )

    def _load_stored_image(self, payload: Mapping[str, object]) -> tuple[bytes, str]:
        raw_media_id = _payload_string(payload, "media_id")
        try:
            media_id = UUID(raw_media_id)
        except ValueError as error:
            raise InboundPayloadError("投递图片缺少有效的内部媒体标识") from error
        with self._session_factory() as session:
            media = session.get(StoredMedia, media_id)
            if media is None:
                raise InboundPayloadError("投递图片对应的媒体记录不存在")
            object_key = media.object_key
            mime_type = media.mime_type
        try:
            data = self._storage.get(object_key)
        except Exception as error:
            raise InboundPayloadError("投递图片对象读取失败") from error
        if not data:
            raise InboundPayloadError("投递图片对象为空")
        extension = mimetypes.guess_extension(mime_type, strict=False) or ".jpg"
        return data, f"{media_id}{extension}"


def _mapping(value: object, error_message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InboundPayloadError(error_message)
    return cast(Mapping[str, object], value)


def _required_callback_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InboundPayloadError(f"企业微信回调缺少有效的 {key}")
    return value.strip()


def _payload_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InboundPayloadError(f"企业微信投递缺少有效的 {key}")
    return value


def _nonempty(value: str, setting_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{setting_name} 不能为空")
    return value.strip()


def _text_content(value: object) -> str:
    text = _mapping(value, "文字消息缺少 text 内容")
    content = text.get("content")
    if not isinstance(content, str):
        raise InboundPayloadError("文字消息内容无效")
    return content


def _quote_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return None
    msgtype = value.get("msgtype")
    if msgtype == "text":
        try:
            return _text_content(value.get("text"))
        except InboundPayloadError:
            return None
    if msgtype == "mixed":
        mixed = value.get("mixed")
        if not isinstance(mixed, Mapping):
            return None
        items = mixed.get("msg_item")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            return None
        contents: list[str] = []
        for item in items:
            if isinstance(item, Mapping) and item.get("msgtype") == "text":
                try:
                    contents.append(_text_content(item.get("text")))
                except InboundPayloadError:
                    continue
        return "\n".join(contents) or None
    if msgtype == "template_card" or "template_card" in value:
        card = value.get("template_card")
        if not isinstance(card, Mapping):
            return None
        contents: list[str] = []
        main_title = card.get("main_title")
        if isinstance(main_title, Mapping):
            title = main_title.get("title")
            if isinstance(title, str) and title.strip():
                contents.append(title)
        subtitle = card.get("sub_title_text")
        if isinstance(subtitle, str) and subtitle.strip():
            contents.append(subtitle)
        return "\n".join(contents) or None
    return None


def _contains_identifier(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, Mapping):
        return any(
            isinstance(value.get(key), str) and value[key] == expected
            for key in ("userid", "user_id", "id", "aibotid")
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_identifier(item, expected) for item in value)
    return False


def _image_mime_type(
    data: bytes, filename: str | None, url: str, supplied_mime_type: object
) -> str:
    if isinstance(supplied_mime_type, str) and supplied_mime_type.startswith("image/"):
        return supplied_mime_type
    candidates = [filename or "", urlparse(url).path]
    for candidate in candidates:
        guessed, _ = mimetypes.guess_type(candidate)
        if guessed and guessed.startswith("image/"):
            return guessed
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise InboundPayloadError("下载内容不是受支持的图片")


def _copy_frame(frame: Mapping[str, object]) -> dict[str, object]:
    copied: dict[str, object] = {}
    for key, value in frame.items():
        if isinstance(value, Mapping):
            copied[key] = _copy_frame(cast(Mapping[str, object], value))
        elif isinstance(value, list):
            copied[key] = [_copy_value(item) for item in value]
        else:
            copied[key] = value
    return copied


def _copy_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _copy_frame(cast(Mapping[str, object], value))
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    return value


def _reply_card(reply: InboundReply) -> dict[str, object]:
    actions: list[dict[str, object]] = []
    for action in reply.actions[:3]:
        parsed = urlparse(action.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise TransportError("企业微信回复卡片包含无效跳转地址")
        actions.append({"type": 1, "title": action.label[:13], "url": action.url})
    if not actions:
        raise TransportError("企业微信回复卡片缺少有效操作")
    return {
        "card_type": "text_notice",
        "main_title": {"title": reply.title[:26], "desc": reply.text[:30]},
        "jump_list": actions,
        "card_action": {"type": 1, "url": actions[0]["url"]},
        "task_id": f"kefu_reply_{uuid4().hex}",
    }


def _platform_result(result: Mapping[str, object]) -> dict[str, object]:
    """Persist acknowledgement metadata, never callback content or secrets."""
    metadata: dict[str, object] = {"wecom": "accepted"}
    for result_field in ("errcode", "errmsg"):
        if result_field in result:
            metadata[result_field] = result[result_field]
    headers = result.get("headers")
    if isinstance(headers, Mapping) and isinstance(headers.get("req_id"), str):
        metadata["platform_req_id"] = headers["req_id"]
    return metadata
