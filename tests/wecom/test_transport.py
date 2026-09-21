from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Mapping

import pytest

from kefu.media.storage import InMemoryObjectStorage, MediaIngestor
from kefu.persistence.models import DeliveryDestination, PartKind
from kefu.wecom.aibot_client import OfficialAiBotClient
from kefu.wecom.transport import (
    InboundEvent,
    InboundImagePart,
    InboundReply,
    InboundTextPart,
    LongConnectionWeComAdapter,
    OutboundMessage,
    ReplyAction,
    TransportError,
    _quote_text,
)
from tests.conftest import DeskContext


class FakeAiBotClient:
    def __init__(self) -> None:
        self.is_connected = False
        self.handlers: dict[str, list[Callable[..., object]]] = defaultdict(list)
        self.downloads: dict[str, tuple[bytes, str | None]] = {}
        self.download_calls: list[tuple[str, str | None]] = []
        self.markdowns: list[dict[str, str]] = []
        self.template_cards: list[dict[str, object]] = []
        self.uploads: list[tuple[bytes, str]] = []
        self.images: list[dict[str, str]] = []
        self.replies: list[dict[str, object]] = []

    def on(self, event: str, listener: Callable[..., object]) -> object:
        self.handlers[event].append(listener)
        return listener

    async def connect(self) -> object:
        self.is_connected = True
        self.emit("authenticated")
        return self

    def disconnect(self) -> None:
        self.is_connected = False

    def emit(self, event: str, *args: object) -> None:
        for listener in self.handlers[event]:
            listener(*args)

    async def download_file(self, url: str, aes_key: str | None = None) -> tuple[bytes, str | None]:
        self.download_calls.append((url, aes_key))
        return self.downloads[url]

    async def send_markdown(
        self, *, chatid: str, req_id: str, content: str
    ) -> Mapping[str, object]:
        self.markdowns.append({"chatid": chatid, "req_id": req_id, "content": content})
        return {"headers": {"req_id": req_id}, "errcode": 0}

    async def send_template_card(
        self, *, chatid: str, req_id: str, card: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.template_cards.append({"chatid": chatid, "req_id": req_id, "card": dict(card)})
        return {"headers": {"req_id": req_id}, "errcode": 0}

    async def send_image(
        self, *, chatid: str, req_id: str, media_id: str
    ) -> Mapping[str, object]:
        self.images.append({"chatid": chatid, "req_id": req_id, "media_id": media_id})
        return {"headers": {"req_id": req_id}, "errcode": 0}

    async def upload_image(self, *, data: bytes, filename: str) -> str:
        self.uploads.append((data, filename))
        return f"wecom-media-{len(self.uploads)}"

    async def reply_markdown(
        self, *, frame: Mapping[str, object], content: str
    ) -> Mapping[str, object]:
        self.replies.append({"kind": "markdown", "frame": dict(frame), "content": content})
        return {"headers": {"req_id": "inbound-1"}, "errcode": 0}

    async def reply_card(
        self, *, frame: Mapping[str, object], card: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.replies.append({"kind": "card", "frame": dict(frame), "card": dict(card)})
        return {"headers": {"req_id": "inbound-1"}, "errcode": 0}


class HangingAiBotClient(FakeAiBotClient):
    def __init__(self) -> None:
        super().__init__()
        self.connect_cancelled = False

    async def connect(self) -> object:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.connect_cancelled = True
            raise


def _adapter(
    desk_context: DeskContext, storage: InMemoryObjectStorage, client: FakeAiBotClient
) -> LongConnectionWeComAdapter:
    return LongConnectionWeComAdapter(
        bot_id="aibot-1",
        secret="secret-1",
        session_factory=desk_context.session_factory,
        storage=storage,
        bot_mention_name="事件机器人",
        client=client,
    )


def test_long_connection_normalizes_mixed_callback_and_decrypts_images(
    desk_context: DeskContext,
) -> None:
    storage = InMemoryObjectStorage()
    client = FakeAiBotClient()
    client.downloads["https://wecom.example/image"] = (
        b"\x89PNG\r\n\x1a\ncontents",
        "capture.bin",
    )
    adapter = _adapter(desk_context, storage, client)
    frame = {
        "headers": {"req_id": "callback-1"},
        "body": {
            "msgid": "message-1",
            "msgtype": "mixed",
            "from": {"userid": "dev-a"},
            "chatid": "dev-chat",
            "chattype": "group",
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "@事件机器人 已处理"}},
                    {
                        "msgtype": "image",
                        "image": {"url": "https://wecom.example/image", "aeskey": "key-1"},
                    },
                    {"msgtype": "text", "text": {"content": "请查收"}},
                ]
            },
            "quote": {
                "msgtype": "mixed",
                "mixed": {
                    "msg_item": [
                        {"msgtype": "text", "text": {"content": "〔KF·ABCDEFG〕"}},
                    ]
                },
            },
        },
    }

    async def receive() -> InboundEvent:
        await adapter.start()
        stream = adapter.events()
        client.emit("message", frame)
        event = await anext(stream)
        await stream.aclose()
        await adapter.close()
        return event

    event = asyncio.run(receive())
    assert event.msgid == "message-1"
    assert event.mentioned_bot is True
    assert event.quote_content == "〔KF·ABCDEFG〕"
    assert event.parts == (
        InboundTextPart("@事件机器人 已处理"),
        InboundImagePart(b"\x89PNG\r\n\x1a\ncontents", "image/png"),
        InboundTextPart("请查收"),
    )
    assert client.download_calls == [("https://wecom.example/image", "key-1")]


def test_long_connection_can_delay_passive_reply_and_reuse_callback_req_id(
    desk_context: DeskContext,
) -> None:
    storage = InMemoryObjectStorage()
    client = FakeAiBotClient()
    adapter = _adapter(desk_context, storage, client)
    frame = {
        "headers": {"req_id": "callback-delayed-1"},
        "body": {
            "msgid": "message-delayed-1",
            "msgtype": "text",
            "from": {"userid": "dev-a"},
            "chatid": "dev-chat",
            "chattype": "group",
            "text": {"content": "@事件机器人 请咨询研发"},
        },
    }
    consultant_replied = asyncio.Event()

    async def scenario() -> str:
        await adapter.start()
        stream = adapter.events()
        try:
            client.emit("message", frame)
            inbound = await anext(stream)
            raw_frame = inbound.metadata["wecom_raw_frame"]
            assert isinstance(raw_frame, Mapping)
            saved_req_id = raw_frame["headers"]["req_id"]
            assert isinstance(saved_req_id, str)

            async def delayed_passive_reply() -> None:
                await consultant_replied.wait()
                await adapter.reply(inbound, InboundReply(text="咨询侧已回复"))

            reply_task = asyncio.create_task(delayed_passive_reply())
            await asyncio.sleep(0)
            assert client.replies == []
            assert not reply_task.done()

            consultant_replied.set()
            await reply_task
            return saved_req_id
        finally:
            await stream.aclose()
            await adapter.close()

    saved_req_id = asyncio.run(scenario())
    assert client.replies == [
        {
            "kind": "markdown",
            "frame": frame,
            "content": "咨询侧已回复",
        }
    ]
    assert client.replies[0]["frame"]["headers"]["req_id"] == saved_req_id


def test_quote_text_extracts_marker_from_template_card() -> None:
    assert _quote_text(
        {
            "msgtype": "template_card",
            "template_card": {
                "main_title": {"title": "〔KF·ABCDEFG〕"},
                "sub_title_text": "<@dev-a>\n客户无法登录",
            },
        }
    ) == "〔KF·ABCDEFG〕\n<@dev-a>\n客户无法登录"


def test_long_connection_requires_an_explicit_group_mention(desk_context: DeskContext) -> None:
    storage = InMemoryObjectStorage()
    client = FakeAiBotClient()
    adapter = _adapter(desk_context, storage, client)
    frame = {
        "headers": {"req_id": "callback-2"},
        "body": {
            "msgid": "message-2",
            "msgtype": "text",
            "from": {"userid": "dev-a"},
            "chatid": "dev-chat",
            "chattype": "group",
            "text": {"content": "这是内部讨论"},
        },
    }

    async def receive() -> InboundEvent:
        await adapter.start()
        stream = adapter.events()
        client.emit("message", frame)
        event = await anext(stream)
        await stream.aclose()
        await adapter.close()
        return event

    assert asyncio.run(receive()).mentioned_bot is False


def test_long_connection_times_out_when_sdk_connect_retries_forever(
    desk_context: DeskContext,
) -> None:
    storage = InMemoryObjectStorage()
    client = HangingAiBotClient()
    adapter = LongConnectionWeComAdapter(
        bot_id="aibot-1",
        secret="secret-1",
        session_factory=desk_context.session_factory,
        storage=storage,
        connect_timeout_seconds=0.05,
        client=client,
    )

    async def start() -> None:
        with pytest.raises(TransportError, match="认证超时"):
            await adapter.start()

    asyncio.run(start())
    assert client.connect_cancelled is True
    assert client.is_connected is False


def test_long_connection_uploads_stored_image_and_reuses_delivery_req_id(
    desk_context: DeskContext,
) -> None:
    storage = InMemoryObjectStorage()
    media_id = MediaIngestor(desk_context.session_factory, storage).ingest(
        InboundImagePart(data=b"\x89PNG\r\n\x1a\noriginal", mime_type="image/png")
    )
    client = FakeAiBotClient()
    adapter = _adapter(desk_context, storage, client)

    async def send() -> None:
        await adapter.start()
        text_receipt = await adapter.send(
            OutboundMessage(
                destination_type=DeliveryDestination.CHAT,
                destination_address="dev-chat",
                req_id="persisted-text-req",
                kind=PartKind.TEXT,
                payload={"content": "研发问题〔KF·ABCDEFG〕"},
            )
        )
        image_receipt = await adapter.send(
            OutboundMessage(
                destination_type=DeliveryDestination.USER,
                destination_address="consult-a",
                req_id="persisted-image-req",
                kind=PartKind.IMAGE,
                payload={"media_id": str(media_id)},
            )
        )
        await adapter.close()
        assert text_receipt.platform_result["platform_req_id"] == "persisted-text-req"
        assert image_receipt.platform_result["platform_req_id"] == "persisted-image-req"

    asyncio.run(send())
    assert client.markdowns == [
        {
            "chatid": "dev-chat",
            "req_id": "persisted-text-req",
            "content": "研发问题〔KF·ABCDEFG〕",
        }
    ]
    assert client.uploads == [(b"\x89PNG\r\n\x1a\noriginal", f"{media_id}.png")]
    assert client.images == [
        {
            "chatid": "consult-a",
            "req_id": "persisted-image-req",
            "media_id": "wecom-media-1",
        }
    ]


def test_long_connection_sends_quoteable_markdown_with_persisted_req_id(
    desk_context: DeskContext,
) -> None:
    storage = InMemoryObjectStorage()
    client = FakeAiBotClient()
    adapter = _adapter(desk_context, storage, client)
    content = "<@dev-a>\n客户无法登录\n\n[查看事件历史](https://events.example.test/events/ABCDEFG)\n\n〔KF·ABCDEFG〕"

    async def send() -> None:
        await adapter.start()
        receipt = await adapter.send(
            OutboundMessage(
                destination_type=DeliveryDestination.CHAT,
                destination_address="dev-chat",
                req_id="persisted-markdown-req",
                kind=PartKind.TEXT,
                payload={"content": content},
            )
        )
        await adapter.close()
        assert receipt.platform_result["platform_req_id"] == "persisted-markdown-req"

    asyncio.run(send())
    assert client.markdowns == [
        {"chatid": "dev-chat", "req_id": "persisted-markdown-req", "content": content}
    ]


def test_long_connection_sends_active_template_card_with_persisted_req_id(
    desk_context: DeskContext,
) -> None:
    storage = InMemoryObjectStorage()
    client = FakeAiBotClient()
    adapter = _adapter(desk_context, storage, client)
    card = {
        "card_type": "text_notice",
        "main_title": {"title": "登录失败", "desc": "发言人：咨询甲"},
        "sub_title_text": "〔KF·ABCDEFG〕",
        "jump_list": [
            {
                "type": 1,
                "title": "查看事件历史",
                "url": "https://events.example.test/events/ABCDEFG",
            }
        ],
        "card_action": {"type": 1, "url": "https://events.example.test/events/ABCDEFG"},
        "task_id": "kefu_history_stable-task-id",
    }

    async def send() -> None:
        await adapter.start()
        receipt = await adapter.send(
            OutboundMessage(
                destination_type=DeliveryDestination.CHAT,
                destination_address="dev-chat",
                req_id="persisted-card-req",
                kind=PartKind.TEXT,
                payload={"template_card": card},
            )
        )
        await adapter.close()
        assert receipt.platform_result["platform_req_id"] == "persisted-card-req"

    asyncio.run(send())
    assert client.template_cards == [
        {"chatid": "dev-chat", "req_id": "persisted-card-req", "card": card}
    ]


def test_long_connection_replies_to_the_callback_with_a_navigation_card(
    desk_context: DeskContext,
) -> None:
    storage = InMemoryObjectStorage()
    client = FakeAiBotClient()
    adapter = _adapter(desk_context, storage, client)
    event = InboundEvent(
        msgid="message-3",
        sender_userid="consult-a",
        chatid=None,
        chattype="single",
        parts=(InboundTextPart("问题"),),
        metadata={"wecom_raw_frame": {"headers": {"req_id": "callback-3"}}},
    )

    async def reply() -> None:
        await adapter.start()
        await adapter.reply(
            event,
            InboundReply(
                text="已保存为草稿",
                actions=(ReplyAction("创建新事件", "https://events.example.test/drafts/one/new"),),
            ),
        )
        await adapter.close()

    asyncio.run(reply())
    assert client.replies[0]["kind"] == "card"
    card = client.replies[0]["card"]
    assert isinstance(card, dict)
    assert card["jump_list"] == [
        {"type": 1, "title": "创建新事件", "url": "https://events.example.test/drafts/one/new"}
    ]


class RawManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], str]] = []

    async def send_reply(
        self, req_id: str, body: dict[str, object], command: str
    ) -> Mapping[str, object]:
        self.calls.append((req_id, body, command))
        if command == "aibot_upload_media_init":
            return {"body": {"upload_id": "upload-1"}}
        if command == "aibot_upload_media_finish":
            return {"body": {"media_id": "wecom-media-1"}}
        return {"headers": {"req_id": req_id}, "errcode": 0}


class RawSdk:
    def __init__(self) -> None:
        self._ws_manager = RawManager()
        self.is_connected = True


def test_official_client_uses_required_media_upload_frames() -> None:
    sdk = RawSdk()
    client = OfficialAiBotClient(bot_id="bot", secret="secret", sdk_client=sdk)
    data = b"a" * (512 * 1024 + 1)

    async def upload() -> str:
        return await client.upload_image(data=data, filename="image.png")

    assert asyncio.run(upload()) == "wecom-media-1"
    commands = [call[2] for call in sdk._ws_manager.calls]
    assert commands == [
        "aibot_upload_media_init",
        "aibot_upload_media_chunk",
        "aibot_upload_media_chunk",
        "aibot_upload_media_finish",
    ]
    chunk_bodies = [
        call[1] for call in sdk._ws_manager.calls if call[2] == "aibot_upload_media_chunk"
    ]
    assert {body["chunk_index"] for body in chunk_bodies} == {0, 1}


def test_official_client_sends_template_card_frame() -> None:
    sdk = RawSdk()
    client = OfficialAiBotClient(bot_id="bot", secret="secret", sdk_client=sdk)
    card = {
        "card_type": "text_notice",
        "main_title": {"title": "〔KF·ABCDEFG〕"},
        "sub_title_text": "客户无法登录",
        "jump_list": [],
        "card_action": {"type": 1, "url": "https://events.example.test/events/ABCDEFG"},
    }

    async def send() -> Mapping[str, object]:
        return await client.send_template_card(
            chatid="dev-chat", req_id="persisted-card-req", card=card
        )

    assert asyncio.run(send())["errcode"] == 0
    assert sdk._ws_manager.calls == [
        (
            "persisted-card-req",
            {"chatid": "dev-chat", "msgtype": "template_card", "template_card": card},
            "aibot_send_msg",
        )
    ]
