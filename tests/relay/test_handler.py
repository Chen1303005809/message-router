from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from kefu.case_desk.contracts import Actor, CreateCase, TextPart
from kefu.media.storage import InMemoryObjectStorage, MediaIngestor
from kefu.persistence.models import (
    EntrySide,
    MembershipRole,
    MessageDraft,
    PartKind,
    TeamMembership,
    WaitingOn,
    WeComChannel,
)
from kefu.relay.delivery import DeliveryWorker
from kefu.relay.handler import Relay, RelayDisposition
from kefu.wecom.transport import (
    FakeWeComAdapter,
    InboundEvent,
    InboundImagePart,
    InboundTextPart,
)
from tests.conftest import DeskContext


def test_relay_runs_draft_to_group_reply_round_trip_with_idempotency(
    desk_context: DeskContext,
) -> None:
    storage = InMemoryObjectStorage()
    relay = Relay(
        desk_context.session_factory,
        desk_context.desk,
        media_ingestor=MediaIngestor(desk_context.session_factory, storage),
    )
    original = InboundEvent(
        msgid="consult-inbound-1",
        sender_userid="consult-a",
        chatid=None,
        chattype="single",
        parts=(
            InboundTextPart("客户无法登录"),
            InboundImagePart(data=b"screenshot", mime_type="image/png"),
            InboundTextPart("请协助定位"),
        ),
    )
    draft = relay.handle(original)
    assert draft.disposition is RelayDisposition.DRAFT_SAVED
    assert draft.draft_id is not None
    assert len(storage.objects) == 1
    replay = relay.handle(original)
    assert replay.idempotent is True
    assert replay.draft_id == draft.draft_id

    created = desk_context.desk.execute(
        CreateCase(
            title="登录失败",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            draft_id=draft.draft_id,
        ),
        Actor(desk_context.users["consult_a"]),
    )
    transport = FakeWeComAdapter()
    asyncio.run(DeliveryWorker(desk_context.desk, transport).deliver_pending())
    quote = next(
        message.payload["content"]
        for message in transport.sent
        if message.kind is PartKind.TEXT
    )
    developer_reply = InboundEvent(
        msgid="dev-inbound-1",
        sender_userid="dev-b",
        chatid="chat-dev-a",
        chattype="group",
        parts=(
            InboundTextPart("已找到配置问题"),
            InboundImagePart(data=b"fix", mime_type="image/png"),
        ),
        quote_content=str(quote),
        mentioned_bot=True,
    )
    forwarded = relay.handle(developer_reply)
    assert forwarded.disposition is RelayDisposition.FORWARDED
    assert forwarded.case_ref == created.case_ref
    duplicate_reply = relay.handle(developer_reply)
    assert duplicate_reply.idempotent is True

    pending = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["consult_a"])
    )
    assert pending.entries[-1].side is EntrySide.DEV
    assert [part.kind for part in pending.entries[-1].parts] == [PartKind.TEXT, PartKind.IMAGE]
    assert pending.waiting_on is WaitingOn.DEV
    asyncio.run(DeliveryWorker(desk_context.desk, transport).deliver_pending())
    delivered = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["consult_a"])
    )
    assert delivered.waiting_on is WaitingOn.CONSULT


def test_relay_rejects_unquoted_or_wrong_group_developer_reply(desk_context: DeskContext) -> None:
    relay = Relay(desk_context.session_factory, desk_context.desk)
    unquoted = relay.handle(
        InboundEvent(
            msgid="dev-unquoted",
            sender_userid="dev-a",
            chatid="chat-dev-a",
            chattype="group",
            parts=(InboundTextPart("没有引用"),),
            mentioned_bot=True,
        )
    )
    assert unquoted.disposition is RelayDisposition.REJECTED

    created = desk_context.desk.execute(
        CreateCase(
            title="群聊校验",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            parts=(TextPart("请处理"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    wrong_group = relay.handle(
        InboundEvent(
            msgid="dev-wrong-group",
            sender_userid="dev-a",
            chatid="chat-dev-b",
            chattype="group",
            parts=(InboundTextPart("错误群的回复"),),
            quote_content=f"〔KF·{created.case_ref}〕",
            mentioned_bot=True,
        )
    )
    assert wrong_group.disposition is RelayDisposition.REJECTED


def test_consult_group_selects_consult_side_for_dual_role_member(
    desk_context: DeskContext,
) -> None:
    with desk_context.session_factory() as session, session.begin():
        session.add(
            TeamMembership(
                team_id=desk_context.teams["consult"],
                user_id=desk_context.users["dev_a"],
                role=MembershipRole.MEMBER,
            )
        )
        session.add(
            WeComChannel(
                team_id=desk_context.teams["consult"],
                chatid="chat-consult",
                initialized_at=datetime.now(UTC),
            )
        )
    created = desk_context.desk.execute(
        CreateCase(
            title="咨询群身份判定",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            parts=(TextPart("原始问题"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    relay = Relay(desk_context.session_factory, desk_context.desk)

    decision = relay.handle(
        InboundEvent(
            msgid="dual-role-consult-group-reply",
            sender_userid="dev-a",
            chatid="chat-consult",
            chattype="group",
            parts=(InboundTextPart("咨询侧补充信息"),),
            quote_content=f"〔KF·{created.case_ref}〕",
            mentioned_bot=True,
        )
    )

    assert decision.disposition is RelayDisposition.FORWARDED
    case = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["dev_a"])
    )
    assert case.entries[-1].side is EntrySide.CONSULT


class BrokenObjectStorage:
    def put(self, object_key: str, data: bytes, *, mime_type: str) -> None:
        raise OSError("simulated object-store outage")


def test_media_storage_failure_does_not_create_partial_draft(desk_context: DeskContext) -> None:
    relay = Relay(
        desk_context.session_factory,
        desk_context.desk,
        media_ingestor=MediaIngestor(desk_context.session_factory, BrokenObjectStorage()),
    )
    decision = relay.handle(
        InboundEvent(
            msgid="failed-media",
            sender_userid="consult-a",
            chatid=None,
            chattype="single",
            parts=(
                InboundTextPart("文字仍然存在"),
                InboundImagePart(data=b"not-stored", mime_type="image/png"),
            ),
        )
    )
    assert decision.disposition is RelayDisposition.REJECTED
    with desk_context.session_factory() as session:
        assert session.scalars(select(MessageDraft)).all() == []


def test_team_admin_can_bind_the_current_wecom_group(desk_context: DeskContext) -> None:
    with desk_context.session_factory() as session, session.begin():
        membership = session.get(
            TeamMembership,
            {"team_id": desk_context.teams["dev_a"], "user_id": desk_context.users["dev_a"]},
        )
        assert membership is not None
        membership.role = MembershipRole.ADMIN
    relay = Relay(desk_context.session_factory, desk_context.desk)

    decision = relay.handle(
        InboundEvent(
            msgid="bind-dev-group",
            sender_userid="dev-a",
            chatid="chat-dev-a-replacement",
            chattype="group",
            parts=(InboundTextPart("@事件机器人 绑定研发团队 研发一组"),),
            mentioned_bot=True,
        )
    )

    assert decision.disposition is RelayDisposition.CHANNEL_BOUND
    with desk_context.session_factory() as session:
        channels = session.scalars(
            select(WeComChannel).where(WeComChannel.team_id == desk_context.teams["dev_a"])
        ).all()
    assert [channel.chatid for channel in channels if channel.active] == ["chat-dev-a-replacement"]
