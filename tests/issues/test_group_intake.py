from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from kefu.case_desk.contracts import Actor, CreateCase, TextPart
from kefu.config import Settings
from kefu.issues.group_intake import GroupIssueIntakeService
from kefu.issues.service import IssueManagement
from kefu.media.storage import InMemoryObjectStorage, MediaIngestor
from kefu.persistence.models import (
    Case,
    CaseEntry,
    CaseEntryPart,
    Delivery,
    DeliveryDestination,
    GroupIssueIntakeSession,
    GroupIssueIntakeTurn,
    IssueModule,
    PartKind,
    StoredMedia,
    WeComChannel,
)
from kefu.relay.handler import Relay
from kefu.wecom.transport import (
    FakeWeComAdapter,
    InboundEvent,
    InboundImagePart,
    InboundTextPart,
)
from kefu.worker import _inbound_loop


class FakeGroupAgent:
    def __init__(
        self,
        *,
        fill_initial: bool = True,
        fail_once: bool = False,
    ) -> None:
        self.fill_initial = fill_initial
        self.fail_once = fail_once
        self.calls = 0

    async def draft_group_issue(
        self,
        original_text,
        conversation,
        current_fields,
        acceptance_teams,
        *,
        request_id=None,
    ):
        del request_id
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("planned Agent failure")
        fields = dict(current_fields)
        latest = next(
            (item["content"] for item in reversed(conversation) if item["role"] == "user"),
            "",
        )
        if not fields.get("issue_title"):
            fields["issue_title"] = "行情查询报错"
        if self.fill_initial and not current_fields.get("issue_title"):
            fields["issue_description"] = "行情查询时报错，影响盘中使用。"
            fields["acceptance_team_id"] = acceptance_teams[0]["id"]
            fields["suspected_type"] = IssueModule.INTRADAY.value
        if "修改为研发二组" in latest:
            fields["acceptance_team_id"] = acceptance_teams[1]["id"]
        if "修改为外部研发群" in latest:
            fields["acceptance_team_id"] = ""
        if "补上描述和模块" in latest:
            fields["issue_description"] = "风控页面查询时报错。"
            fields["suspected_type"] = IssueModule.RISK_CONTROL.value
        return fields


@pytest.fixture
def group_intake(desk_context):
    with desk_context.session_factory() as session, session.begin():
        session.add(
            WeComChannel(
                team_id=desk_context.teams["consult"],
                chatid="chat-consult",
                initialized_at=datetime.now(UTC),
            )
        )
    storage = InMemoryObjectStorage()
    agent = FakeGroupAgent()
    service = GroupIssueIntakeService(
        desk_context.session_factory,
        IssueManagement(desk_context.session_factory, desk_context.desk, Settings.from_env()),
        agent,
        MediaIngestor(desk_context.session_factory, storage),
        storage,
    )
    return service, agent, storage


def _event(
    msgid: str,
    *,
    parts=(),
    chatid="chat-consult",
    quote=None,
    sender_userid=None,
) -> InboundEvent:
    return InboundEvent(
        msgid=msgid,
        chatid=chatid,
        chattype="group",
        parts=tuple(parts),
        quote_content=quote,
        mentioned_bot=True,
        sender_userid=sender_userid,
    )


def test_group_intake_turn_keeps_member_author_snapshot_and_unknown_fallback(
    desk_context, group_intake
):
    service, _agent, _storage = group_intake
    asyncio.run(
        service.handle(
            _event(
                "known-author-turn",
                parts=(InboundTextPart("有文字的咨询问题"),),
                sender_userid="consult-a",
            )
        )
    )
    asyncio.run(
        service.handle(
            _event(
                "encrypted-author-turn",
                parts=(InboundTextPart("另一条咨询问题"),),
                sender_userid="opaque-encrypted-userid",
            )
        )
    )

    with desk_context.session_factory() as session:
        known = session.scalar(
            select(GroupIssueIntakeTurn).where(
                GroupIssueIntakeTurn.msgid == "known-author-turn"
            )
        )
        encrypted = session.scalar(
            select(GroupIssueIntakeTurn).where(
                GroupIssueIntakeTurn.msgid == "encrypted-author-turn"
            )
        )
        assert known is not None
        assert known.actor_user_id == desk_context.users["consult_a"]
        assert known.actor_name_snapshot == "咨询甲"
        assert encrypted is not None
        assert encrypted.actor_user_id is None
        assert encrypted.actor_name_snapshot is None


def test_group_intake_requires_confirmation_and_dispatches_original_order(
    desk_context, group_intake
):
    service, _agent, storage = group_intake
    original = _event(
        "source-1",
        parts=(
            InboundTextPart("行情查询报错"),
            InboundImagePart(b"image bytes", "image/png"),
            InboundTextPart("截图见上图"),
        ),
    )

    proposal = asyncio.run(service.handle(original))

    assert "问题受理提案（尚未派发）" in proposal
    assert "〔KF-INTAKE:" in proposal
    assert "问题标题：**行情查询报错**" in proposal
    assert "目标研发群：**研发一组**" in proposal
    with desk_context.session_factory() as session:
        assert session.scalar(select(func.count(Case.id))) == 0
        intake_session = session.scalar(select(GroupIssueIntakeSession))
        assert intake_session is not None
        assert intake_session.raw_text == "行情查询报错[图片]截图见上图"
        assert [part["kind"] for part in intake_session.parts_json] == [
            "text",
            "image",
            "text",
        ]

    confirmation = _event(
        "confirm-1",
        parts=(InboundTextPart("确认派发"),),
        quote=proposal,
    )
    result = asyncio.run(service.handle(confirmation))

    assert "目标研发群：**研发一组**" in result
    assert "问题编号：**" in result
    with desk_context.session_factory() as session:
        issue = session.scalar(select(Case))
        assert issue is not None
        issue_ref = issue.case_ref
        assert issue.creator_id is None
        assert issue.current_consultant_id is None
        assert issue.consult_queue_id == desk_context.teams["consult"]
        assert issue.consult_handler_name == "咨询队列"
        assert issue.initial_acceptance_team_id == desk_context.teams["dev_a"]
        assert issue.suspected_type == IssueModule.INTRADAY.value
        entry = session.scalar(
            select(CaseEntry).where(CaseEntry.case_id == issue.id)
        )
        assert entry is not None
        stored_parts = session.scalars(
            select(CaseEntryPart)
            .where(CaseEntryPart.entry_id == entry.id)
            .order_by(CaseEntryPart.position)
        ).all()
        assert [part.kind for part in stored_parts] == [
            PartKind.TEXT,
            PartKind.IMAGE,
            PartKind.TEXT,
        ]
        deliveries = session.scalars(select(Delivery)).all()
        assert len(deliveries) == 1
        assert deliveries[0].destination_type is DeliveryDestination.CHAT
        assert deliveries[0].destination_address == "chat-dev-a"
    issue_view = desk_context.desk.get_case(issue_ref, Actor(desk_context.users["consult_a"]))
    assert issue_view.first_recorder_id is None
    assert issue_view.first_recorder_name == "咨询队列"

    assert len(storage.objects) == 1
    assert asyncio.run(service.handle(confirmation)) == result
    assert asyncio.run(service.handle(original)) == result
    with desk_context.session_factory() as session:
        assert session.scalar(select(func.count(Case.id))) == 1
        assert session.scalar(select(func.count(Delivery.id))) == 1


def test_worker_routes_unquoted_consult_group_mention_into_intake(
    desk_context, group_intake
):
    service, _agent, _storage = group_intake
    transport = FakeWeComAdapter()
    transport.push_inbound(
        _event("worker-intake", parts=(InboundTextPart("行情异常"),))
    )

    asyncio.run(
        _inbound_loop(
            relay=Relay(desk_context.session_factory, desk_context.desk),
            transport=transport,
            group_intake=service,
        )
    )

    assert len(transport.replies) == 1
    assert "问题受理提案（尚未派发）" in transport.replies[0][1].text
    with desk_context.session_factory() as session:
        assert session.scalar(select(func.count(Case.id))) == 0


def test_worker_keeps_existing_issue_quote_on_relay_path(desk_context, group_intake):
    service, agent, _storage = group_intake
    created = desk_context.desk.execute(
        CreateCase(
            title="已有问题",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            parts=(TextPart("原始问题"),),
            suspected_type=IssueModule.INTRADAY.value,
        ),
        Actor(desk_context.users["consult_a"]),
    )
    transport = FakeWeComAdapter()
    transport.push_inbound(
        _event(
            "existing-case-followup",
            chatid="chat-dev-a",
            quote=f"〔KF·{created.case_ref}〕",
            parts=(InboundTextPart("研发跟进"),),
        )
    )

    asyncio.run(
        _inbound_loop(
            relay=Relay(desk_context.session_factory, desk_context.desk),
            transport=transport,
            group_intake=service,
        )
    )

    with desk_context.session_factory() as session:
        issue = session.scalar(select(Case).where(Case.case_ref == created.case_ref))
        assert issue is not None
        assert issue.last_entry_sequence == 2
    assert agent.calls == 0


def test_group_intake_lists_missing_options_and_applies_edit_with_confirmation(
    desk_context, group_intake
):
    service, agent, _storage = group_intake
    agent.fill_initial = False
    initial = _event("source-missing", parts=(InboundTextPart("风控页面异常"),))

    proposal = asyncio.run(service.handle(initial))

    assert "待补齐" in proposal
    assert "可选研发群：**研发一组**、**研发二组**" in proposal
    assert "可选模块：" in proposal
    followup = _event(
        "followup-confirm",
        parts=(
            InboundTextPart("修改为研发二组，补上描述和模块，然后确认派发"),
        ),
        quote=proposal,
    )

    result = asyncio.run(service.handle(followup))

    assert "目标研发群：**研发二组**" in result
    with desk_context.session_factory() as session:
        issue = session.scalar(select(Case))
        assert issue is not None
        assert issue.initial_acceptance_team_id == desk_context.teams["dev_b"]
        assert issue.issue_description == "风控页面查询时报错。"
        assert issue.suspected_type == IssueModule.RISK_CONTROL.value
        delivery = session.scalar(select(Delivery))
        assert delivery is not None
        assert delivery.destination_address == "chat-dev-b"


def test_unknown_retarget_clears_old_target_and_confirmation_stays_blocked(
    desk_context, group_intake
):
    service, _agent, _storage = group_intake
    proposal = asyncio.run(
        service.handle(_event("source-retarget", parts=(InboundTextPart("行情异常"),)))
    )
    changed = asyncio.run(
        service.handle(
            _event(
                "change-invalid-target",
                parts=(InboundTextPart("修改为外部研发群"),),
                quote=proposal,
            )
        )
    )

    assert "目标研发群：**未确定**" in changed
    assert "待补齐" in changed
    still_blocked = asyncio.run(
        service.handle(
            _event("confirm-invalid-target", parts=(InboundTextPart("确认派发"),), quote=changed)
        )
    )

    assert "暂未派发" in still_blocked
    with desk_context.session_factory() as session:
        assert session.scalar(select(func.count(Case.id))) == 0


def test_group_intake_rejects_cross_group_invalid_and_unbound_references(
    desk_context, group_intake
):
    service, _agent, _storage = group_intake
    proposal = asyncio.run(service.handle(
        _event("source-route", parts=(InboundTextPart("行情查询异常"),))
    ))
    marker_start = proposal.index("〔KF-INTAKE:")
    marker = proposal[marker_start : proposal.index("〕", marker_start) + 1]

    cross_group = asyncio.run(service.handle(
        _event(
            "cross-group",
            chatid="chat-dev-a",
            quote=marker,
            parts=(InboundTextPart("确认派发"),),
        )
    ))
    invalid = asyncio.run(service.handle(
        _event("invalid-marker", quote="〔KF-INTAKE:bad〕", parts=(InboundTextPart("确认派发"),))
    ))
    unbound = asyncio.run(service.handle(
        _event("unbound-start", chatid="unknown-chat", parts=(InboundTextPart("新问题"),))
    ))
    image_only = asyncio.run(service.handle(
        _event("image-only-start", parts=(InboundImagePart(b"image", "image/png"),))
    ))

    assert "跨群" in cross_group
    assert "标记无效" in invalid
    assert "绑定" in unbound
    assert "文字描述" in image_only
    with desk_context.session_factory() as session:
        assert session.scalar(select(func.count(Case.id))) == 0


def test_group_intake_agent_failure_does_not_dispatch_and_can_retry(
    desk_context, group_intake
):
    service, agent, _storage = group_intake
    agent.fail_once = True
    initial = _event("source-failure", parts=(InboundTextPart("行情查询异常"),))

    proposal = asyncio.run(service.handle(initial))

    assert "尚未应用，也没有派发" in proposal
    with desk_context.session_factory() as session:
        assert session.scalar(select(func.count(Case.id))) == 0
    retry = _event("retry-confirm", parts=(InboundTextPart("确认派发"),), quote=proposal)

    result = asyncio.run(service.handle(retry))

    assert "目标研发群：**研发一组**" in result
    assert agent.calls == 2
    with desk_context.session_factory() as session:
        assert session.scalar(select(func.count(Case.id))) == 1


def test_expired_session_cleans_orphan_image_objects(desk_context, group_intake):
    service, _agent, storage = group_intake
    proposal = asyncio.run(service.handle(
        _event(
            "source-expired",
            parts=(InboundTextPart("行情异常"), InboundImagePart(b"orphan image", "image/png")),
        )
    ))
    assert "〔KF-INTAKE:" in proposal
    with desk_context.session_factory() as session, session.begin():
        intake_session = session.scalar(select(GroupIssueIntakeSession))
        assert intake_session is not None
        orphan_media_id = intake_session.parts_json[1]["media_id"]
        intake_session.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    expired_reply = asyncio.run(
        service.handle(
            _event(
                "expired-followup",
                parts=(InboundTextPart("确认派发"),),
                quote=proposal,
            )
        )
    )
    assert "已过期" in expired_reply
    with desk_context.session_factory() as session:
        assert session.scalar(select(func.count(Case.id))) == 0

    removed = asyncio.run(service.cleanup_expired())

    assert removed == 1
    assert storage.objects == {}
    with desk_context.session_factory() as session:
        assert session.scalar(select(func.count(GroupIssueIntakeSession.id))) == 0
        assert session.scalar(select(func.count(GroupIssueIntakeTurn.id))) == 0
        assert session.get(StoredMedia, UUID(orphan_media_id)) is None

