from __future__ import annotations

import asyncio

from kefu.case_desk.contracts import Actor, CreateCase, TextPart
from kefu.persistence.models import DeliveryDestination
from kefu.relay.delivery import DeliveryWorker
from kefu.relay.handler import Relay
from kefu.relay.references import parse_quoted_case_ref
from kefu.wecom.deferred_reply import DeferredPassiveReplyStore
from kefu.wecom.transport import FakeWeComAdapter, InboundEvent, InboundReply, InboundTextPart
from kefu.worker import _inbound_loop
from tests.conftest import DeskContext


def test_inbound_loop_turns_a_saved_draft_into_a_callback_navigation_card(
    desk_context: DeskContext,
) -> None:
    relay = Relay(desk_context.session_factory, desk_context.desk)
    transport = FakeWeComAdapter()
    transport.push_inbound(
        InboundEvent(
            msgid="consult-draft-worker",
            sender_userid="consult-a",
            chatid=None,
            chattype="single",
            parts=(InboundTextPart("客户问题"),),
        )
    )

    asyncio.run(
        _inbound_loop(
            relay=relay,
            transport=transport,
            web_base_url="https://events.example.test",
        )
    )

    assert len(transport.replies) == 1
    _, reply = transport.replies[0]
    assert reply.title == "消息草稿已保存"
    assert reply.actions[0].url.startswith("https://events.example.test/drafts/")


def test_inbound_loop_replies_to_the_saved_group_callback_after_consultant_message(
    desk_context: DeskContext,
) -> None:
    created = desk_context.desk.execute(
        CreateCase(
            title="延迟被动回复实验",
            consult_queue_id=desk_context.teams["consult"],
            developer_id=desk_context.users["dev_a"],
            parts=(TextPart("原始问题"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    asyncio.run(DeliveryWorker(desk_context.desk, FakeWeComAdapter()).deliver_pending())
    marker = f"〔KF·{created.case_ref}〕"
    group_event = InboundEvent(
        msgid="developer-delayed-callback",
        sender_userid="dev-a",
        chatid="chat-dev-a",
        chattype="group",
        parts=(InboundTextPart("研发回复"),),
        quote_content=marker,
        mentioned_bot=True,
        metadata={"wecom_raw_frame": {"headers": {"req_id": "callback-group-1"}}},
    )
    consultant_event = InboundEvent(
        msgid="consultant-delayed-release",
        sender_userid="consult-a",
        chatid=None,
        chattype="single",
        parts=(InboundTextPart("@测试机器人 咨询侧确认内容"),),
        quote_content=marker,
        metadata={"wecom_raw_frame": {"headers": {"req_id": "callback-consult-1"}}},
    )
    transport = FakeWeComAdapter()
    transport.push_inbound(group_event)
    transport.push_inbound(consultant_event)
    deferred_replies = DeferredPassiveReplyStore(desk_context.session_factory, ttl_seconds=60)
    relay = Relay(desk_context.session_factory, desk_context.desk)

    asyncio.run(
        _inbound_loop(
            relay=relay,
            transport=transport,
            web_base_url="https://events.example.test",
            deferred_replies=deferred_replies,
        )
    )

    assert len(deferred_replies) == 0
    assert len(transport.replies) == 1
    replied_event, reply = transport.replies[0]
    assert replied_event.msgid == group_event.msgid
    assert replied_event.metadata == group_event.metadata
    decision = relay.handle(consultant_event)
    assert decision.idempotent is True
    assert decision.delivery_ids
    persisted_content = relay.get_delivery_markdown_content(decision.delivery_ids[0])
    assert persisted_content is not None
    assert reply == InboundReply(text=persisted_content)
    assert "> *指定经办人：研发甲*" in reply.text
    assert "转发人：咨询甲  事件编号：" in reply.text
    assert "@测试机器人" not in reply.text
    assert parse_quoted_case_ref(reply.text) == created.case_ref
    asyncio.run(DeliveryWorker(desk_context.desk, transport).deliver_pending())
    assert all(
        message.destination_type is not DeliveryDestination.CHAT for message in transport.sent
    )


def test_inbound_loop_defers_the_no_quote_prompt_for_the_experiment(
    desk_context: DeskContext,
) -> None:
    created = desk_context.desk.execute(
        CreateCase(
            title="无引用延迟回复实验",
            consult_queue_id=desk_context.teams["consult"],
            developer_id=desk_context.users["dev_a"],
            parts=(TextPart("原始问题"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    asyncio.run(DeliveryWorker(desk_context.desk, FakeWeComAdapter()).deliver_pending())
    marker = f"〔KF·{created.case_ref}〕"
    group_event = InboundEvent(
        msgid="developer-unquoted-delayed-callback",
        sender_userid="dev-a",
        chatid="chat-dev-a",
        chattype="group",
        parts=(InboundTextPart("@事件机器人 无引用测试消息"),),
        mentioned_bot=True,
        metadata={"wecom_raw_frame": {"headers": {"req_id": "callback-unquoted-1"}}},
    )
    consultant_event = InboundEvent(
        msgid="consultant-unquoted-release",
        sender_userid="consult-a",
        chatid=None,
        chattype="single",
        parts=(InboundTextPart("咨询侧释放无引用测试"),),
        quote_content=marker,
        metadata={"wecom_raw_frame": {"headers": {"req_id": "callback-consult-2"}}},
    )
    transport = FakeWeComAdapter()
    transport.push_inbound(group_event)
    transport.push_inbound(consultant_event)
    relay = Relay(desk_context.session_factory, desk_context.desk)

    asyncio.run(
        _inbound_loop(
            relay=relay,
            transport=transport,
            web_base_url="https://events.example.test",
            deferred_replies=DeferredPassiveReplyStore(
                desk_context.session_factory, ttl_seconds=60
            ),
        )
    )

    assert len(transport.replies) == 1
    replied_event, reply = transport.replies[0]
    assert replied_event.msgid == group_event.msgid
    assert replied_event.metadata == group_event.metadata
    decision = relay.handle(consultant_event)
    assert decision.delivery_ids
    assert reply.text == relay.get_delivery_markdown_content(decision.delivery_ids[0])
    assert "> *指定经办人：研发甲*" in reply.text
    assert "转发人：咨询甲  事件编号：" in reply.text
    assert parse_quoted_case_ref(reply.text) == created.case_ref
    asyncio.run(DeliveryWorker(desk_context.desk, transport).deliver_pending())
    assert all(
        message.destination_type is not DeliveryDestination.CHAT for message in transport.sent
    )


class FailingPassiveReplyAdapter(FakeWeComAdapter):
    async def reply(self, event: InboundEvent, reply: InboundReply):
        raise RuntimeError("planned passive reply failure")


def test_inbound_loop_restores_active_group_delivery_when_passive_reply_fails(
    desk_context: DeskContext,
) -> None:
    created = desk_context.desk.execute(
        CreateCase(
            title="被动回复失败兜底",
            consult_queue_id=desk_context.teams["consult"],
            developer_id=desk_context.users["dev_a"],
            parts=(TextPart("原始问题"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    asyncio.run(DeliveryWorker(desk_context.desk, FakeWeComAdapter()).deliver_pending())
    marker = f"〔KF·{created.case_ref}〕"
    transport = FailingPassiveReplyAdapter()
    transport.push_inbound(
        InboundEvent(
            msgid="developer-fallback-callback",
            sender_userid="dev-a",
            chatid="chat-dev-a",
            chattype="group",
            parts=(InboundTextPart("研发回复"),),
            quote_content=marker,
            mentioned_bot=True,
            metadata={"wecom_raw_frame": {"headers": {"req_id": "fallback-group-1"}}},
        )
    )
    transport.push_inbound(
        InboundEvent(
            msgid="consultant-fallback-release",
            sender_userid="consult-a",
            chatid=None,
            chattype="single",
            parts=(InboundTextPart("咨询侧回复"),),
            quote_content=marker,
            metadata={"wecom_raw_frame": {"headers": {"req_id": "fallback-consult-1"}}},
        )
    )
    deferred_replies = DeferredPassiveReplyStore(desk_context.session_factory, ttl_seconds=60)

    asyncio.run(
        _inbound_loop(
            relay=Relay(desk_context.session_factory, desk_context.desk),
            transport=transport,
            web_base_url="https://events.example.test",
            deferred_replies=deferred_replies,
        )
    )

    assert len(deferred_replies) == 0
    asyncio.run(DeliveryWorker(desk_context.desk, transport).deliver_pending())
    group_markdown = next(
        message.payload["content"]
        for message in transport.sent
        if message.destination_type is DeliveryDestination.CHAT
        and "content" in message.payload
    )
    assert "> *指定经办人：研发甲*" in str(group_markdown)
    assert "转发人：咨询甲  事件编号：" in str(group_markdown)
    assert parse_quoted_case_ref(str(group_markdown)) == created.case_ref
