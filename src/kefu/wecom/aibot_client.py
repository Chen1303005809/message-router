"""A narrow wrapper around WeCom's long-connection Python SDK.

The SDK owns socket authentication, heartbeats, reconnects and AES media
decryption.  This wrapper adds the small media-upload surface that is present
in the official Node SDK but is not yet exposed by version 1.0.2 of the Python
SDK.  Keeping that compatibility code here prevents private SDK details from
leaking into the relay or domain modules.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Callable, Mapping
from typing import Protocol, cast
from uuid import uuid4


class AiBotClient(Protocol):
    """Operations the WeCom transport needs from a connected bot client."""

    @property
    def is_connected(self) -> bool: ...

    def on(self, event: str, listener: Callable[..., object]) -> object: ...

    async def connect(self) -> object: ...

    def disconnect(self) -> None: ...

    async def download_file(
        self, url: str, aes_key: str | None = None
    ) -> tuple[bytes, str | None]: ...

    async def send_markdown(
        self, *, chatid: str, req_id: str, content: str
    ) -> Mapping[str, object]: ...

    async def send_template_card(
        self, *, chatid: str, req_id: str, card: Mapping[str, object]
    ) -> Mapping[str, object]: ...

    async def send_image(
        self, *, chatid: str, req_id: str, media_id: str
    ) -> Mapping[str, object]: ...

    async def upload_image(self, *, data: bytes, filename: str) -> str: ...

    async def reply_markdown(
        self, *, frame: Mapping[str, object], content: str
    ) -> Mapping[str, object]: ...

    async def reply_card(
        self, *, frame: Mapping[str, object], card: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class AiBotClientError(RuntimeError):
    """The SDK could not complete a WeCom protocol operation."""


class OfficialAiBotClient:
    """Production client backed by ``wecom-aibot-python-sdk``.

    A persisted delivery ``req_id`` is passed through to ``aibot_send_msg``.
    That lets the delivery worker correlate retries with WeCom acknowledgements
    instead of allowing the SDK to create an unrelated request id per attempt.
    """

    _UPLOAD_CHUNK_SIZE = 512 * 1024
    _UPLOAD_MAX_CHUNKS = 100
    _UPLOAD_CHUNK_RETRIES = 2

    def __init__(
        self,
        *,
        bot_id: str,
        secret: str,
        ws_url: str | None = None,
        sdk_client: object | None = None,
    ) -> None:
        if sdk_client is None:
            try:
                from aibot import WSClient, WSClientOptions
            except ImportError as error:  # pragma: no cover - dependency is declared.
                raise AiBotClientError(
                    "缺少企业微信长连接 SDK；请安装 wecom-aibot-python-sdk"
                ) from error
            sdk_client = WSClient(
                WSClientOptions(
                    bot_id=bot_id,
                    secret=secret,
                    ws_url=ws_url or "",
                    max_reconnect_attempts=-1,
                )
            )
        self._client = sdk_client

    @property
    def is_connected(self) -> bool:
        return bool(getattr(self._client, "is_connected", False))

    def on(self, event: str, listener: Callable[..., object]) -> object:
        callback_registrar = getattr(self._client, "on", None)
        if not callable(callback_registrar):
            raise AiBotClientError("企业微信 SDK 缺少事件监听能力")
        return callback_registrar(event, listener)

    async def connect(self) -> object:
        connector = getattr(self._client, "connect", None)
        if not callable(connector):
            raise AiBotClientError("企业微信 SDK 缺少连接能力")
        result = connector()
        if hasattr(result, "__await__"):
            return await result
        return result

    def disconnect(self) -> None:
        disconnector = getattr(self._client, "disconnect", None)
        if not callable(disconnector):
            raise AiBotClientError("企业微信 SDK 缺少断开连接能力")
        disconnector()

    async def download_file(self, url: str, aes_key: str | None = None) -> tuple[bytes, str | None]:
        downloader = getattr(self._client, "download_file", None)
        if not callable(downloader):
            raise AiBotClientError("企业微信 SDK 缺少媒体下载能力")
        result = downloader(url, aes_key)
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[0], bytes):
            raise AiBotClientError("企业微信 SDK 返回了无效的媒体下载结果")
        filename = result[1] if isinstance(result[1], str) else None
        return result[0], filename

    async def send_markdown(
        self, *, chatid: str, req_id: str, content: str
    ) -> Mapping[str, object]:
        return await self._send_raw(
            req_id=req_id,
            command="aibot_send_msg",
            body={
                "chatid": chatid,
                "msgtype": "markdown",
                "markdown": {"content": content},
            },
        )

    async def send_template_card(
        self, *, chatid: str, req_id: str, card: Mapping[str, object]
    ) -> Mapping[str, object]:
        return await self._send_raw(
            req_id=req_id,
            command="aibot_send_msg",
            body={
                "chatid": chatid,
                "msgtype": "template_card",
                "template_card": dict(card),
            },
        )

    async def send_image(
        self, *, chatid: str, req_id: str, media_id: str
    ) -> Mapping[str, object]:
        return await self._send_raw(
            req_id=req_id,
            command="aibot_send_msg",
            body={
                "chatid": chatid,
                "msgtype": "image",
                "image": {"media_id": media_id},
            },
        )

    async def upload_image(self, *, data: bytes, filename: str) -> str:
        if not data:
            raise AiBotClientError("不能上传空图片")
        total_chunks = (len(data) + self._UPLOAD_CHUNK_SIZE - 1) // self._UPLOAD_CHUNK_SIZE
        if total_chunks > self._UPLOAD_MAX_CHUNKS:
            raise AiBotClientError("图片超过企业微信临时素材上传上限")

        init = await self._send_raw(
            req_id=self._generated_req_id("aibot_upload_media_init"),
            command="aibot_upload_media_init",
            body={
                "type": "image",
                "filename": filename,
                "total_size": len(data),
                "total_chunks": total_chunks,
                "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
            },
        )
        init_body = _mapping(init.get("body"))
        upload_id = _required_string(init_body, "upload_id", "企业微信未返回上传会话标识")

        async def upload_chunk(index: int) -> None:
            start = index * self._UPLOAD_CHUNK_SIZE
            chunk = data[start : start + self._UPLOAD_CHUNK_SIZE]
            body = {
                "upload_id": upload_id,
                # The official Node SDK uses a zero-based index in live frames.
                "chunk_index": index,
                "base64_data": base64.b64encode(chunk).decode("ascii"),
            }
            last_error: Exception | None = None
            for attempt in range(self._UPLOAD_CHUNK_RETRIES + 1):
                try:
                    await self._send_raw(
                        req_id=self._generated_req_id("aibot_upload_media_chunk"),
                        command="aibot_upload_media_chunk",
                        body=body,
                    )
                    return
                except Exception as error:  # The final failure is surfaced to durable delivery.
                    last_error = error
                    if attempt < self._UPLOAD_CHUNK_RETRIES:
                        await asyncio.sleep(0.5 * (attempt + 1))
            assert last_error is not None
            raise AiBotClientError(f"图片分片 {index + 1} 上传失败") from last_error

        concurrency = total_chunks if total_chunks <= 4 else 3 if total_chunks <= 10 else 2
        iterator = iter(range(total_chunks))

        async def worker() -> None:
            while True:
                try:
                    index = next(iterator)
                except StopIteration:
                    return
                await upload_chunk(index)

        await asyncio.gather(*(worker() for _ in range(concurrency)))
        completed = await self._send_raw(
            req_id=self._generated_req_id("aibot_upload_media_finish"),
            command="aibot_upload_media_finish",
            body={"upload_id": upload_id},
        )
        completed_body = _mapping(completed.get("body"))
        return _required_string(completed_body, "media_id", "企业微信未返回临时素材标识")

    async def reply_markdown(
        self, *, frame: Mapping[str, object], content: str
    ) -> Mapping[str, object]:
        responder = getattr(self._client, "reply_stream", None)
        if not callable(responder):
            raise AiBotClientError("企业微信 SDK 缺少被动回复能力")
        result = responder(
            dict(frame),
            self._generated_req_id("kefu_stream"),
            content,
            True,
        )
        if hasattr(result, "__await__"):
            result = await result
        return _mapping(result)

    async def reply_card(
        self, *, frame: Mapping[str, object], card: Mapping[str, object]
    ) -> Mapping[str, object]:
        responder = getattr(self._client, "reply_template_card", None)
        if not callable(responder):
            raise AiBotClientError("企业微信 SDK 缺少模板卡片回复能力")
        result = responder(dict(frame), dict(card))
        if hasattr(result, "__await__"):
            result = await result
        return _mapping(result)

    async def _send_raw(
        self, *, req_id: str, command: str, body: Mapping[str, object]
    ) -> Mapping[str, object]:
        manager = getattr(self._client, "_ws_manager", None)
        sender = getattr(manager, "send_reply", None)
        if not callable(sender):
            raise AiBotClientError("当前企业微信 Python SDK 不支持所需的长连接发送能力")
        try:
            result = sender(req_id, dict(body), command)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as error:
            raise AiBotClientError(f"企业微信 {command} 请求失败") from error
        return _mapping(result)

    @staticmethod
    def _generated_req_id(prefix: str) -> str:
        return f"{prefix}_{uuid4().hex}"


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AiBotClientError("企业微信 SDK 返回了无效协议报文")
    return cast(Mapping[str, object], value)


def _required_string(data: Mapping[str, object], key: str, error_message: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AiBotClientError(error_message)
    return value
