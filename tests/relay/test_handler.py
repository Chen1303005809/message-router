from __future__ import annotations

from sqlalchemy import select

from kefu.case_desk.contracts import Actor, CreateCase, TextPart
from kefu.media.storage import MediaIngestor
from kefu.persistence.models import (
    EntrySide,
    IssueIntakeDraft,
    MembershipRole,
    TeamMembership,
    WeComChannel,
)
from kefu.relay.handler import Relay, RelayDisposition
from kefu.wecom.transport import (
    InboundEvent,
    InboundImagePart,
    InboundTextPart,
)
from tests.conftest import DeskContext


def test_relay_routes_new_private_issues_to_the_h5_handoff_flow(
    desk_context: DeskContext,
) -> None:
    relay = Relay(desk_context.session_factory, desk_context.desk)
    original = InboundEvent(
        msgid="consult-inbound-1",
        sender_userid="consult-a",
        chatid=None,
        chattype="single",
        parts=(
            InboundTextPart("客户无法登录"),
            InboundTextPart("请协助定位"),
        ),
    )
    rejected = relay.handle(original)
    assert rejected.disposition is RelayDisposition.REJECTED
    assert rejected.reply_text is not None and "H5" in rejected.reply_text
    replay = relay.handle(original)
    assert replay.idempotent is True


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
            suspected_type="盘中",
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


def test_relay_allows_unregistered_member_to_reply_in_dev_group(
    desk_context: DeskContext,
) -> None:
    created = desk_context.desk.execute(
        CreateCase(
            title="未注册成员回复",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            suspected_type="盘中",
            parts=(TextPart("请研发协助处理"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    relay = Relay(desk_context.session_factory, desk_context.desk)

    decision = relay.handle(
        InboundEvent(
            msgid="unregistered-dev-reply",
            sender_userid="wecom-not-in-directory",
            chatid="chat-dev-a",
            chattype="group",
            parts=(InboundTextPart("已完成定位"),),
            quote_content=f"〔KF·{created.case_ref}〕",
            mentioned_bot=True,
        )
    )

    assert decision.disposition is RelayDisposition.FORWARDED
    case = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["consult_a"])
    )
    entry = next(
        entry for entry in reversed(case.entries) if entry.kind.value == "formal_message"
    )
    assert entry.side is EntrySide.DEV
    assert entry.actor_user_id is None
    assert entry.actor_name_snapshot == "一组负责人"


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
    created = desk_context.desk.execute(
        CreateCase(
            title="咨询群身份判定",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            suspected_type="盘中",
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
    case = desk_context.desk.get_case(created.case_ref or "", Actor(desk_context.users["dev_a"]))
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
        assert session.scalars(select(IssueIntakeDraft)).all() == []


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
