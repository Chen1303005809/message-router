from __future__ import annotations

import asyncio
import logging

from kefu.media.storage import InMemoryObjectStorage
from kefu.persistence.models import DeliveryDestination, PartKind
from kefu.wecom.transport import (
    InboundReply,
    LongConnectionWeComAdapter,
    OutboundMessage,
)


class FakeClient:
    def __init__(self) -> None:
        self.card_replies: list[dict[str, object]] = []
        self.sent_destinations: list[str] = []
        self.is_connected = True

    async def download_file(self, _url, _aes_key=None):
        return b"", None

    async def reply_template_card(self, *, frame, card):
        self.card_replies.append(dict(card))
        return {"errcode": 0}

    async def send_markdown(self, *, chatid, req_id, content):
        self.sent_destinations.append(chatid)
        return {"errcode": 0}


def _adapter(client: FakeClient) -> LongConnectionWeComAdapter:
    return LongConnectionWeComAdapter(
        bot_id="bot-test",
        secret="secret-test",
        session_factory=lambda: None,
        storage=InMemoryObjectStorage(),
        client=client,
    )


def test_single_frame_preserves_sender_and_parses_text_without_logging_contents(caplog):
    adapter = _adapter(FakeClient())
    frame = {
        "headers": {"req_id": "request-private"},
        "body": {
            "msgid": "private-1",
            "chattype": "single",
            "from": {"userid": "plain-member-id"},
            "msgtype": "text",
            "text": {"content": "private body must not appear in logs"},
            "response_url": "temporary-response-credential",
        },
    }
    event = asyncio.run(adapter._normalize_frame(frame))

    assert event.sender_userid == "plain-member-id"
    assert event.chatid is None
    assert event.parts[0].text == "private body must not appear in logs"
    with caplog.at_level(logging.INFO):
        adapter._log_inbound_frame(frame)
    assert "plain-member-id" not in caplog.text
    assert "private body must not appear in logs" not in caplog.text
    assert "temporary-response-credential" not in caplog.text


def test_passive_template_card_reply_uses_the_original_callback_context():
    client = FakeClient()
    adapter = _adapter(client)
    frame = {
        "headers": {"req_id": "request-private"},
        "body": {"msgid": "private-2", "chattype": "single"},
    }
    frame["body"].update(
        {
            "msgtype": "text",
            "text": {"content": "open card"},
        }
    )
    event = asyncio.run(adapter._normalize_frame(frame))
    card = {
        "card_type": "text_notice",
        "main_title": {"title": "继续提交"},
        "card_action": {"type": 1, "url": "https://example.test/handoff"},
    }

    asyncio.run(
        adapter.reply(
            event,
            InboundReply(text="打开 H5", template_card=card),
        )
    )

    assert client.card_replies == [card]


def test_user_delivery_passes_registered_userid_to_sdk_destination_slot():
    client = FakeClient()
    adapter = _adapter(client)
    adapter._started = True
    adapter._ready.set()

    asyncio.run(
        adapter.send(
            OutboundMessage(
                destination_type=DeliveryDestination.USER,
                destination_address="consultant-plain-userid",
                req_id="durable-copy-1",
                kind=PartKind.TEXT,
                payload={"content": "研发更新"},
            )
        )
    )

    assert client.sent_destinations == ["consultant-plain-userid"]
