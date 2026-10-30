from __future__ import annotations

import asyncio
from dataclasses import replace
from urllib.parse import urlsplit

from sqlalchemy import select

from kefu.case_desk.contracts import Actor, ImagePart, TextPart
from kefu.config import Settings
from kefu.issues.service import IssueManagement
from kefu.media.storage import InMemoryObjectStorage, MediaIngestor
from kefu.persistence.models import PrivateIntakeHandoff
from kefu.relay.handler import Relay
from kefu.wecom.transport import (
    FakeWeComAdapter,
    InboundEvent,
    InboundImagePart,
    InboundTextPart,
)
from kefu.worker import _inbound_loop


def test_private_message_creates_owner_bound_h5_card_without_exposing_body_in_url(
    desk_context,
):
    storage = InMemoryObjectStorage()
    ingestor = MediaIngestor(desk_context.session_factory, storage)
    settings = replace(
        Settings.from_env(),
        web_base_url="https://issues.example.test",
    )
    issues = IssueManagement(
        desk_context.session_factory,
        desk_context.desk,
        settings,
        storage=storage,
    )
    relay = Relay(
        desk_context.session_factory,
        desk_context.desk,
        media_ingestor=ingestor,
    )
    transport = FakeWeComAdapter()
    transport.push_inbound(
        InboundEvent(
            msgid="private-new-issue",
            chatid=None,
            chattype="single",
            parts=(
                InboundTextPart("查询页面点击保存后一直转圈"),
                InboundImagePart(b"private image bytes", "image/png"),
                InboundTextPart("请看截图"),
            ),
            sender_userid="consult-a",
        )
    )

    asyncio.run(
        _inbound_loop(
            relay=relay,
            transport=transport,
            private_intake=issues,
            media_ingestor=ingestor,
            settings=settings,
        )
    )

    assert len(transport.replies) == 1
    reply = transport.replies[0][1]
    assert reply.template_card is not None
    action = reply.template_card["card_action"]
    assert isinstance(action, dict)
    url = str(action["url"])
    assert "查询页面点击保存后一直转圈" not in url
    parsed_url = urlsplit(url)
    assert parsed_url.query == ""
    assert parsed_url.path.endswith("/issues/private-intake")
    token = parsed_url.fragment
    handoff = issues.get_private_intake_handoff(
        token,
        Actor(desk_context.users["consult_a"]),
    )
    assert handoff.raw_text == "查询页面点击保存后一直转圈[图片]请看截图"
    assert len(handoff.parts) == 3
    assert handoff.parts[0] == TextPart("查询页面点击保存后一直转圈")
    assert isinstance(handoff.parts[1], ImagePart)
    assert handoff.parts[2] == TextPart("请看截图")

    with desk_context.session_factory() as session:
        stored = session.scalar(
            select(PrivateIntakeHandoff).where(PrivateIntakeHandoff.msgid == "private-new-issue")
        )
        assert stored is not None
        assert stored.actor_id == desk_context.users["consult_a"]


def test_private_my_issues_command_returns_personal_list_link(desk_context):
    settings = replace(
        Settings.from_env(),
        web_base_url="https://issues.example.test",
    )
    issues = IssueManagement(
        desk_context.session_factory,
        desk_context.desk,
        settings,
    )
    relay = Relay(desk_context.session_factory, desk_context.desk)
    transport = FakeWeComAdapter()
    transport.push_inbound(
        InboundEvent(
            msgid="private-my-issues",
            chatid=None,
            chattype="single",
            parts=(InboundTextPart("我的问题"),),
            sender_userid="consult-a",
        )
    )

    asyncio.run(
        _inbound_loop(
            relay=relay,
            transport=transport,
            private_intake=issues,
            settings=settings,
        )
    )

    card = transport.replies[0][1].template_card
    assert card is not None
    action = card["card_action"]
    assert isinstance(action, dict)
    assert str(action["url"]) == "https://issues.example.test/issues?state=mine"
