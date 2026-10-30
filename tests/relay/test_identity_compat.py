from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from kefu.case_desk.contracts import Actor, CreateCase, TextPart, TransferConsultant
from kefu.persistence.models import (
    Case,
    CaseEntry,
    Delivery,
    DeliveryDestination,
    DeliveryItem,
    DeliveryStatus,
    EntrySide,
    TeamMembership,
    WaitingOn,
    WeComChannel,
)
from kefu.relay.delivery import DeliveryWorker
from kefu.relay.handler import Relay, RelayDisposition
from kefu.wecom.transport import FakeWeComAdapter, InboundEvent, InboundTextPart


def _bind_consult_chat(desk_context) -> None:
    with desk_context.session_factory() as session, session.begin():
        existing = session.scalar(
            select(WeComChannel.id).where(
                WeComChannel.team_id == desk_context.teams["consult"]
            )
        )
        if existing is None:
            session.add(
                WeComChannel(
                    team_id=desk_context.teams["consult"],
                    chatid="chat-consult",
                    initialized_at=datetime.now(UTC),
                )
            )


def _create_issue(desk_context):
    return desk_context.desk.execute(
        CreateCase(
            title="接口超时",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            parts=(TextPart("接口请求超时"),),
            suspected_type="盘中",
        ),
        Actor(desk_context.users["consult_a"]),
    )


def test_group_plain_userid_is_attributed_only_when_member_of_bound_team(desk_context):
    _bind_consult_chat(desk_context)
    created = _create_issue(desk_context)
    relay = Relay(desk_context.session_factory, desk_context.desk)

    decision = relay.handle(
        InboundEvent(
            msgid="group-member-reply",
            chatid="chat-consult",
            chattype="group",
            parts=(InboundTextPart("补充了复现步骤"),),
            quote_content=f"〔KF·{created.case_ref}〕",
            mentioned_bot=True,
            sender_userid="consult-a",
        )
    )

    assert decision.disposition is RelayDisposition.FORWARDED
    with desk_context.session_factory() as session:
        entry = session.scalar(
            select(CaseEntry).where(CaseEntry.source_msgid == "group-member-reply")
        )
        assert entry is not None
        assert entry.actor_user_id == desk_context.users["consult_a"]
        assert entry.actor_name_snapshot == "咨询甲"

    encrypted_sender = relay.handle(
        InboundEvent(
            msgid="group-encrypted-reply",
            chatid="chat-consult",
            chattype="group",
            parts=(InboundTextPart("继续补充"),),
            quote_content=f"〔KF·{created.case_ref}〕",
            mentioned_bot=True,
            sender_userid="opaque-encrypted-userid",
        )
    )
    assert encrypted_sender.disposition is RelayDisposition.FORWARDED
    with desk_context.session_factory() as session:
        entry = session.scalar(
            select(CaseEntry).where(CaseEntry.source_msgid == "group-encrypted-reply")
        )
        assert entry is not None
        assert entry.actor_user_id is None
        assert entry.actor_name_snapshot == "咨询甲"


def test_private_followup_requires_registered_member_and_routes_with_personal_copy(
    desk_context,
):
    _bind_consult_chat(desk_context)
    created = _create_issue(desk_context)
    relay = Relay(desk_context.session_factory, desk_context.desk)

    decision = relay.handle(
        InboundEvent(
            msgid="private-dev-followup",
            chatid=None,
            chattype="single",
            parts=(InboundTextPart("修复已经部署"),),
            quote_content=f"〔KF·{created.case_ref}〕",
            sender_userid="dev-a",
        )
    )

    assert decision.disposition is RelayDisposition.FORWARDED
    assert len(decision.delivery_ids) == 2
    with desk_context.session_factory() as session:
        entry = session.scalar(
            select(CaseEntry).where(CaseEntry.source_msgid == "private-dev-followup")
        )
        assert entry is not None
        assert entry.side is EntrySide.DEV
        assert entry.actor_user_id == desk_context.users["dev_a"]
        assert entry.actor_name_snapshot == "研发甲"
        deliveries = session.scalars(
            select(Delivery).where(Delivery.entry_id == entry.id)
        ).all()
        assert {
            (item.destination_type, item.destination_address)
            for item in deliveries
        } == {
            (DeliveryDestination.CHAT, "chat-consult"),
            (DeliveryDestination.USER, "consult-a"),
        }
        personal_copy = next(
            item for item in deliveries if item.destination_type is DeliveryDestination.USER
        )
        assert personal_copy.waiting_on_after_delivery is None

    unknown = relay.handle(
        InboundEvent(
            msgid="private-unknown-sender",
            chatid=None,
            chattype="single",
            parts=(InboundTextPart("继续处理"),),
            quote_content=f"〔KF·{created.case_ref}〕",
            sender_userid="opaque-encrypted-userid",
        )
    )
    assert unknown.disposition is RelayDisposition.REJECTED
    assert "联系管理员" in (unknown.reply_text or "")
    with desk_context.session_factory() as session:
        assert (
            session.scalar(
                select(CaseEntry.id).where(
                    CaseEntry.source_msgid == "private-unknown-sender"
                )
            )
            is None
        )


def test_private_followup_is_rejected_when_user_is_not_a_case_member(desk_context):
    _bind_consult_chat(desk_context)
    created = _create_issue(desk_context)
    relay = Relay(desk_context.session_factory, desk_context.desk)

    with desk_context.session_factory() as session, session.begin():
        membership = session.scalar(
            select(TeamMembership).where(
                TeamMembership.team_id == desk_context.teams["dev_a"],
                TeamMembership.user_id == desk_context.users["dev_a"],
            )
        )
        assert membership is not None
        membership.valid_until = datetime.now(UTC)

    decision = relay.handle(
        InboundEvent(
            msgid="private-nonmember-followup",
            chatid=None,
            chattype="single",
            parts=(InboundTextPart("无权的跟进"),),
            quote_content=f"〔KF·{created.case_ref}〕",
            sender_userid="dev-a",
        )
    )
    assert decision.disposition is RelayDisposition.REJECTED


def test_personal_copy_failure_does_not_block_consult_group_delivery(desk_context):
    _bind_consult_chat(desk_context)
    created = _create_issue(desk_context)
    adapter = FakeWeComAdapter()
    asyncio.run(DeliveryWorker(desk_context.desk, adapter).deliver_pending())

    relay = Relay(desk_context.session_factory, desk_context.desk)
    decision = relay.handle(
        InboundEvent(
            msgid="private-copy-retry",
            chatid=None,
            chattype="single",
            parts=(InboundTextPart("修复已经部署"),),
            quote_content=f"〔KF·{created.case_ref}〕",
            sender_userid="dev-a",
        )
    )
    with desk_context.session_factory() as session:
        deliveries = session.scalars(
            select(Delivery).where(Delivery.id.in_(decision.delivery_ids))
        ).all()
        personal = next(
            item for item in deliveries if item.destination_type is DeliveryDestination.USER
        )
        personal_item = session.scalar(
            select(DeliveryItem).where(DeliveryItem.delivery_id == personal.id)
        )
        assert personal_item is not None
        adapter.fail_next(personal_item.req_id)

    result = asyncio.run(DeliveryWorker(desk_context.desk, adapter).deliver_pending())

    assert result.failed_bundles == 1
    with desk_context.session_factory() as session:
        refreshed = session.scalars(
            select(Delivery).where(Delivery.id.in_(decision.delivery_ids))
        ).all()
        group = next(
            item for item in refreshed if item.destination_type is DeliveryDestination.CHAT
        )
        personal = next(
            item for item in refreshed if item.destination_type is DeliveryDestination.USER
        )
        assert group.status is DeliveryStatus.SENT
        assert personal.status is DeliveryStatus.PENDING
        case = session.scalar(select(Case).where(Case.case_ref == created.case_ref))
        assert case is not None
        assert case.waiting_on is WaitingOn.CONSULT


def test_consultant_assignment_notice_has_chat_and_user_deliveries(desk_context):
    _bind_consult_chat(desk_context)
    created = _create_issue(desk_context)
    actor = Actor(desk_context.users["consult_a"])
    current = desk_context.desk.get_case(created.case_ref or "", actor)

    result = desk_context.desk.execute(
        TransferConsultant(
            case_ref=created.case_ref or "",
            expected_version=current.version,
            new_consultant_id=desk_context.users["consult_b"],
        ),
        actor,
    )

    with desk_context.session_factory() as session:
        deliveries = session.scalars(
            select(Delivery).where(Delivery.id.in_(result.delivery_ids))
        ).all()
        assert {
            (item.destination_type, item.destination_address)
            for item in deliveries
        } == {
            (DeliveryDestination.CHAT, "chat-consult"),
            (DeliveryDestination.USER, "consult-b"),
        }
