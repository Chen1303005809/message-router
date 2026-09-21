from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from kefu.case_desk.contracts import (
    Actor,
    CaseFilter,
    CloseCase,
    CorrectEntry,
    CreateCase,
    ImagePart,
    PostFormalMessage,
    ReopenCase,
    SetCaseStatus,
    TextPart,
    TransferConsultant,
    TransferDeveloper,
    TransferDevTeam,
)
from kefu.case_desk.errors import Conflict, Forbidden, RoutingUnavailable, ValidationError
from kefu.persistence.models import (
    CaseEntryKind,
    CaseStatus,
    Delivery,
    DeliveryItem,
    DeliveryStatus,
    LifecycleStatus,
    MessageIntent,
    PartKind,
    User,
    WaitingOn,
)
from kefu.relay.delivery import DeliveryWorker
from kefu.relay.references import parse_quoted_case_ref
from kefu.wecom.transport import FakeWeComAdapter
from tests.conftest import DeskContext


def actor(context: DeskContext, name: str) -> Actor:
    return Actor(context.users[name])


def create_case(
    context: DeskContext,
    *,
    developer: str = "dev_a",
    parts: tuple[TextPart | ImagePart, ...] | None = None,
    source_msgid: str | None = "inbound-create-1",
):
    return context.desk.execute(
        CreateCase(
            title="登录失败",
            consult_queue_id=context.teams["consult"],
            developer_id=context.users[developer],
            parts=parts or (TextPart("客户无法登录"),),
            source_msgid=source_msgid,
        ),
        actor(context, "consult_a"),
    )


def deliver(context: DeskContext, adapter: FakeWeComAdapter | None = None) -> FakeWeComAdapter:
    adapter = adapter or FakeWeComAdapter()
    result = asyncio.run(DeliveryWorker(context.desk, adapter).deliver_pending())
    assert result.failed_bundles == 0
    return adapter


def test_create_preserves_mixed_order_and_changes_wait_only_after_delivery(
    desk_context: DeskContext,
) -> None:
    result = create_case(
        desk_context,
        parts=(
            TextPart("第一段文字"),
            ImagePart(desk_context.media_id),
            TextPart("第二段文字"),
            ImagePart(desk_context.media_id),
        ),
    )
    before = desk_context.desk.get_case(result.case_ref or "", actor(desk_context, "consult_a"))
    assert before.lifecycle_status is LifecycleStatus.OPEN
    assert before.status is CaseStatus.PENDING_CONFIRMATION
    assert before.waiting_on is WaitingOn.CONSULT
    assert [part.kind for part in before.entries[0].parts] == [
        PartKind.TEXT,
        PartKind.IMAGE,
        PartKind.TEXT,
        PartKind.IMAGE,
    ]
    assert before.deliveries[0].status is DeliveryStatus.PENDING

    adapter = deliver(desk_context)
    after = desk_context.desk.get_case(result.case_ref or "", actor(desk_context, "consult_a"))
    assert after.waiting_on is WaitingOn.DEV
    assert after.deliveries[0].status is DeliveryStatus.SENT
    assert [message.kind for message in adapter.sent] == [
        PartKind.TEXT,
        PartKind.IMAGE,
        PartKind.IMAGE,
    ]
    assert all("template_card" not in message.payload for message in adapter.sent)
    marker = f"〔KF·{result.case_ref}〕"
    content = adapter.sent[0].payload["content"]
    assert content == (
        "## 登录失败\n\n"
        "> 指定经办人：研发甲\n\n"
        "**第一段文字\n第二段文字**\n\n\n"
        f"> 转发人：咨询甲  事件编号：{marker}"
    )
    assert "<@" not in str(content)
    assert parse_quoted_case_ref(str(content)) == result.case_ref
    assert str(content).count(marker) == 1


def test_developer_can_reply_without_becoming_current_handler(desk_context: DeskContext) -> None:
    created = create_case(desk_context)
    initial_adapter = deliver(desk_context)
    initial_content = initial_adapter.sent[0].payload["content"]
    assert "> 指定经办人：研发甲" in str(initial_content)
    assert "<@" not in str(initial_content)
    current = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "dev_a"))
    posted = desk_context.desk.execute(
        PostFormalMessage(
            case_ref=created.case_ref or "",
            expected_version=current.version,
            parts=(TextPart("已定位到权限配置"), ImagePart(desk_context.media_id)),
        ),
        actor(desk_context, "dev_b"),
    )
    pending = desk_context.desk.get_case(posted.case_ref or "", actor(desk_context, "consult_a"))
    assert pending.waiting_on is WaitingOn.DEV
    assert pending.entries[-1].actor_name_snapshot == "研发乙"
    assert pending.current_developer_id == desk_context.users["dev_a"]

    adapter = deliver(desk_context)
    finished = desk_context.desk.get_case(posted.case_ref or "", actor(desk_context, "consult_a"))
    assert finished.waiting_on is WaitingOn.CONSULT
    assert adapter.sent[-1].destination_address == "consult-a"
    response_content = next(
        message.payload["content"] for message in adapter.sent if "content" in message.payload
    )
    assert response_content == (
        "## 登录失败\n\n"
        "> 指定经办人：咨询甲\n\n"
        "**已定位到权限配置**\n\n\n"
        f"> 发送人：研发乙  事件编号：〔KF·{created.case_ref}〕"
    )
    assert "<@" not in str(response_content)
    assert parse_quoted_case_ref(str(response_content)) == created.case_ref


def test_formal_forward_strips_at_mentions_but_keeps_email_addresses(
    desk_context: DeskContext,
) -> None:
    create_case(
        desk_context,
        parts=(TextPart("@测试机器人 @研发甲 请排查，联系 qa@example.test"),),
    )

    adapter = deliver(desk_context)
    content = str(adapter.sent[0].payload["content"])

    assert "@测试机器人" not in content
    assert "@研发甲" not in content
    assert "qa@example.test" in content


def test_formal_forward_rejects_when_at_mentions_are_the_only_text(
    desk_context: DeskContext,
) -> None:
    with pytest.raises(ValidationError, match="去除 @ 提及后"):
        create_case(
            desk_context,
            parts=(TextPart("@测试机器人"),),
            source_msgid="only-at-mention",
        )


def test_handover_keeps_timeline_and_close_requires_consult_authority(
    desk_context: DeskContext,
) -> None:
    created = create_case(desk_context)
    deliver(desk_context)
    current = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "consult_a"))
    transferred = desk_context.desk.execute(
        TransferConsultant(
            case_ref=created.case_ref or "",
            expected_version=current.version,
            new_consultant_id=desk_context.users["consult_b"],
        ),
        actor(desk_context, "consult_a"),
    )
    notice_adapter = deliver(desk_context)
    notice_content = next(
        message.payload["content"]
        for message in notice_adapter.sent
        if "content" in message.payload
    )
    assert "事件标题：登录失败" in str(notice_content)
    assert "操作人：咨询甲" in str(notice_content)
    notice_card = next(
        message.payload["template_card"]
        for message in notice_adapter.sent
        if "template_card" in message.payload
    )
    assert notice_card["main_title"]["title"] == "登录失败"
    assert notice_card["main_title"]["desc"] == "操作人：咨询甲"
    handed_over = desk_context.desk.get_case(
        transferred.case_ref or "", actor(desk_context, "consult_b")
    )
    assert handed_over.current_consultant_id == desk_context.users["consult_b"]
    assert [entry.kind for entry in handed_over.entries][-1] is CaseEntryKind.TRANSFER_CONSULTANT
    listed_cases = desk_context.desk.list_cases(CaseFilter(), actor(desk_context, "consult_b"))
    assert listed_cases.items[0].case_ref == created.case_ref

    with pytest.raises(Forbidden):
        desk_context.desk.execute(
            CloseCase(case_ref=created.case_ref or "", expected_version=handed_over.version),
            actor(desk_context, "dev_a"),
        )
    desk_context.desk.execute(
        CloseCase(case_ref=created.case_ref or "", expected_version=handed_over.version),
        actor(desk_context, "consult_b"),
    )
    closed = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "consult_b"))
    assert closed.lifecycle_status is LifecycleStatus.CLOSED
    assert closed.status is CaseStatus.CLOSED
    assert closed.waiting_on is WaitingOn.NONE


def test_event_handlers_and_team_admins_can_transfer_within_each_team(
    desk_context: DeskContext,
) -> None:
    created = create_case(desk_context)
    current = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "consult_a"))

    assert current.can_transfer is True
    assert {person.id for person in current.transferable_consultants} == {
        desk_context.users["consult_a"],
        desk_context.users["consult_b"],
        desk_context.users["consult_admin"],
    }
    assert {person.id for person in current.transferable_developers} == {
        desk_context.users["dev_a"],
        desk_context.users["dev_b"],
        desk_context.users["dev_admin"],
    }

    ordinary_consultant = desk_context.desk.get_case(
        created.case_ref or "", actor(desk_context, "consult_b")
    )
    assert ordinary_consultant.can_transfer is False
    assert ordinary_consultant.transferable_consultants == ()
    assert ordinary_consultant.transferable_developers == ()

    with desk_context.session_factory() as session, session.begin():
        global_admin = session.get(User, desk_context.users["outsider"])
        assert global_admin is not None
        global_admin.is_global_admin = True
    global_admin_view = desk_context.desk.get_case(
        created.case_ref or "", actor(desk_context, "outsider")
    )
    assert global_admin_view.can_transfer is False
    assert global_admin_view.transferable_consultants == ()
    assert global_admin_view.transferable_developers == ()

    with pytest.raises(Forbidden):
        desk_context.desk.execute(
            TransferConsultant(
                case_ref=created.case_ref or "",
                expected_version=current.version,
                new_consultant_id=desk_context.users["consult_b"],
            ),
            actor(desk_context, "consult_b"),
        )

    with pytest.raises(Forbidden):
        desk_context.desk.execute(
            TransferDeveloper(
                case_ref=created.case_ref or "",
                expected_version=current.version,
                new_developer_id=desk_context.users["dev_b"],
            ),
            actor(desk_context, "dev_b"),
        )

    with pytest.raises(Forbidden):
        desk_context.desk.execute(
            TransferConsultant(
                case_ref=created.case_ref or "",
                expected_version=current.version,
                new_consultant_id=desk_context.users["consult_b"],
            ),
            actor(desk_context, "outsider"),
        )

    developer_handler_transfer = desk_context.desk.execute(
        TransferConsultant(
            case_ref=created.case_ref or "",
            expected_version=current.version,
            new_consultant_id=desk_context.users["consult_b"],
        ),
        actor(desk_context, "dev_a"),
    )
    consultant_admin_transfer = desk_context.desk.execute(
        TransferDeveloper(
            case_ref=created.case_ref or "",
            expected_version=developer_handler_transfer.case_version or 0,
            new_developer_id=desk_context.users["dev_b"],
        ),
        actor(desk_context, "consult_admin"),
    )
    developer_admin_transfer = desk_context.desk.execute(
        TransferConsultant(
            case_ref=created.case_ref or "",
            expected_version=consultant_admin_transfer.case_version or 0,
            new_consultant_id=desk_context.users["consult_a"],
        ),
        actor(desk_context, "dev_admin"),
    )
    transferred = desk_context.desk.get_case(
        created.case_ref or "", actor(desk_context, "consult_b")
    )
    assert developer_admin_transfer.case_version == transferred.version
    assert transferred.current_consultant_id == desk_context.users["consult_a"]
    assert transferred.current_developer_id == desk_context.users["dev_b"]

    with pytest.raises(RoutingUnavailable, match="不属于目标研发团队"):
        desk_context.desk.execute(
            TransferDeveloper(
                case_ref=created.case_ref or "",
                expected_version=transferred.version,
                new_developer_id=desk_context.users["dev_c"],
            ),
            actor(desk_context, "consult_a"),
        )


def test_workflow_statuses_are_authorized_and_recorded_in_timeline(
    desk_context: DeskContext,
) -> None:
    created = create_case(desk_context)
    current = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "consult_a"))
    assert current.status is CaseStatus.PENDING_CONFIRMATION
    assert current.can_accept_pending is False

    with pytest.raises(Forbidden, match="研发团队成员受理"):
        desk_context.desk.execute(
            SetCaseStatus(
                case_ref=created.case_ref or "",
                expected_version=current.version,
                status=CaseStatus.IN_PROGRESS,
            ),
            actor(desk_context, "consult_a"),
        )

    started = desk_context.desk.execute(
        SetCaseStatus(
            case_ref=created.case_ref or "",
            expected_version=current.version,
            status=CaseStatus.IN_PROGRESS,
        ),
        actor(desk_context, "dev_b"),
    )
    unchanged = desk_context.desk.execute(
        SetCaseStatus(
            case_ref=created.case_ref or "",
            expected_version=started.case_version or 0,
            status=CaseStatus.IN_PROGRESS,
        ),
        actor(desk_context, "dev_b"),
    )
    assert unchanged.idempotent is True
    assert unchanged.entry_id is None

    with pytest.raises(ValidationError, match="初始状态"):
        desk_context.desk.execute(
            SetCaseStatus(
                case_ref=created.case_ref or "",
                expected_version=started.case_version or 0,
                status=CaseStatus.PENDING_CONFIRMATION,
            ),
            actor(desk_context, "dev_b"),
        )

    processing = desk_context.desk.get_case(
        created.case_ref or "", actor(desk_context, "consult_a")
    )
    assert processing.status is CaseStatus.IN_PROGRESS
    assert processing.entries[-1].kind is CaseEntryKind.STATUS_CHANGED
    assert processing.entries[-1].metadata == {
        "from_status": CaseStatus.PENDING_CONFIRMATION.value,
        "to_status": CaseStatus.IN_PROGRESS.value,
    }

    with pytest.raises(Forbidden):
        desk_context.desk.execute(
            SetCaseStatus(
                case_ref=created.case_ref or "",
                expected_version=started.case_version or 0,
                status=CaseStatus.WAITING_CUSTOMER,
            ),
            actor(desk_context, "dev_b"),
        )

    waiting_customer = desk_context.desk.execute(
        SetCaseStatus(
            case_ref=created.case_ref or "",
            expected_version=started.case_version or 0,
            status=CaseStatus.WAITING_CUSTOMER,
        ),
        actor(desk_context, "consult_a"),
    )
    resumed = desk_context.desk.execute(
        SetCaseStatus(
            case_ref=created.case_ref or "",
            expected_version=waiting_customer.case_version or 0,
            status=CaseStatus.IN_PROGRESS,
        ),
        actor(desk_context, "consult_a"),
    )
    suspended = desk_context.desk.execute(
        SetCaseStatus(
            case_ref=created.case_ref or "",
            expected_version=resumed.case_version or 0,
            status=CaseStatus.SUSPENDED,
        ),
        actor(desk_context, "dev_a"),
    )
    resumed_again = desk_context.desk.execute(
        SetCaseStatus(
            case_ref=created.case_ref or "",
            expected_version=suspended.case_version or 0,
            status=CaseStatus.IN_PROGRESS,
        ),
        actor(desk_context, "dev_a"),
    )
    case = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "dev_a"))
    assert case.status is CaseStatus.IN_PROGRESS
    assert [entry.kind for entry in case.entries[-5:]] == [
        CaseEntryKind.STATUS_CHANGED,
        CaseEntryKind.STATUS_CHANGED,
        CaseEntryKind.STATUS_CHANGED,
        CaseEntryKind.STATUS_CHANGED,
        CaseEntryKind.STATUS_CHANGED,
    ]
    assert resumed_again.case_version == case.version


def test_versions_and_source_msgids_prevent_duplicate_business_entries(
    desk_context: DeskContext,
) -> None:
    created = create_case(desk_context, source_msgid="same-callback")
    replay = create_case(desk_context, source_msgid="same-callback")
    assert replay.idempotent is True
    assert replay.case_ref == created.case_ref
    current = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "consult_a"))
    assert len(current.entries) == 1
    deliver(desk_context)
    current = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "consult_a"))

    with pytest.raises(Conflict):
        desk_context.desk.execute(
            TransferConsultant(
                case_ref=created.case_ref or "",
                expected_version=current.version - 1,
                new_consultant_id=desk_context.users["consult_b"],
            ),
            actor(desk_context, "consult_a"),
        )


def test_image_first_bundle_does_not_send_event_card(desk_context: DeskContext) -> None:
    created = create_case(
        desk_context,
        parts=(ImagePart(desk_context.media_id), TextPart("图片后的补充说明")),
    )
    adapter = deliver(desk_context)
    assert [message.kind for message in adapter.sent] == [
        PartKind.TEXT,
        PartKind.IMAGE,
    ]
    assert all("template_card" not in message.payload for message in adapter.sent)
    content = adapter.sent[0].payload["content"]
    assert content == (
        "## 登录失败\n\n"
        "> 指定经办人：研发甲\n\n"
        "**图片后的补充说明**\n\n\n"
        f"> 转发人：咨询甲  事件编号：〔KF·{created.case_ref}〕"
    )
    assert str(content).count(f"〔KF·{created.case_ref}〕") == 1


def test_sync_does_not_change_waiting_side_after_success(desk_context: DeskContext) -> None:
    created = create_case(desk_context)
    deliver(desk_context)
    current = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "dev_a"))
    desk_context.desk.execute(
        PostFormalMessage(
            case_ref=created.case_ref or "",
            expected_version=current.version,
            parts=(TextPart("正在复现，先同步进展"),),
            intent=MessageIntent.SYNC,
        ),
        actor(desk_context, "dev_b"),
    )
    deliver(desk_context)
    synchronized = desk_context.desk.get_case(
        created.case_ref or "", actor(desk_context, "consult_a")
    )
    assert synchronized.waiting_on is WaitingOn.DEV


def test_waiting_for_me_filter_respects_the_viewers_team_side(desk_context: DeskContext) -> None:
    created = create_case(desk_context)
    deliver(desk_context)
    consult_cases = desk_context.desk.list_cases(
        CaseFilter(waiting_for_me=True), actor(desk_context, "consult_a")
    )
    developer_cases = desk_context.desk.list_cases(
        CaseFilter(waiting_for_me=True), actor(desk_context, "dev_a")
    )
    assert consult_cases.items == ()
    assert [item.case_ref for item in developer_cases.items] == [created.case_ref]


def test_overview_counts_visible_cases_by_lifecycle_and_waiting_side(
    desk_context: DeskContext,
) -> None:
    created = create_case(desk_context)
    before_delivery = desk_context.desk.overview(actor(desk_context, "consult_a"))
    assert before_delivery.open_count == 1
    assert before_delivery.waiting_consult_count == 1
    assert before_delivery.waiting_dev_count == 0
    assert before_delivery.closed_count == 0

    deliver(desk_context)
    after_delivery = desk_context.desk.overview(actor(desk_context, "consult_a"))
    assert after_delivery.open_count == 1
    assert after_delivery.waiting_consult_count == 0
    assert after_delivery.waiting_dev_count == 1

    current = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "consult_a"))
    desk_context.desk.execute(
        CloseCase(case_ref=created.case_ref or "", expected_version=current.version),
        actor(desk_context, "consult_a"),
    )
    after_close = desk_context.desk.overview(actor(desk_context, "consult_a"))
    assert after_close.open_count == 0
    assert after_close.waiting_consult_count == 0
    assert after_close.waiting_dev_count == 0
    assert after_close.closed_count == 1

    outsider = desk_context.desk.overview(actor(desk_context, "outsider"))
    assert outsider.open_count == 0
    assert outsider.closed_count == 0


def test_developer_and_team_transfers_preserve_waiting_and_revoke_old_team(
    desk_context: DeskContext,
) -> None:
    created = create_case(desk_context)
    deliver(desk_context)
    current = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "consult_a"))
    moved_developer = desk_context.desk.execute(
        TransferDeveloper(
            case_ref=created.case_ref or "",
            expected_version=current.version,
            new_developer_id=desk_context.users["dev_b"],
        ),
        actor(desk_context, "consult_a"),
    )
    after_developer = desk_context.desk.get_case(
        created.case_ref or "", actor(desk_context, "consult_a")
    )
    assert after_developer.current_developer_id == desk_context.users["dev_b"]
    assert after_developer.waiting_on is WaitingOn.DEV
    desk_context.desk.execute(
        TransferDevTeam(
            case_ref=moved_developer.case_ref or "",
            expected_version=after_developer.version,
            new_dev_team_id=desk_context.teams["dev_b"],
            new_developer_id=desk_context.users["dev_c"],
        ),
        actor(desk_context, "consult_a"),
    )
    with pytest.raises(Forbidden):
        desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "dev_a"))
    transferred = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "dev_c"))
    assert transferred.current_dev_team_id == desk_context.teams["dev_b"]
    assert transferred.current_developer_id == desk_context.users["dev_c"]
    assert transferred.waiting_on is WaitingOn.DEV


def test_reopen_requires_explicit_next_side(desk_context: DeskContext) -> None:
    created = create_case(desk_context)
    deliver(desk_context)
    current = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "consult_a"))
    closed = desk_context.desk.execute(
        CloseCase(case_ref=created.case_ref or "", expected_version=current.version),
        actor(desk_context, "consult_a"),
    )
    desk_context.desk.execute(
        ReopenCase(
            case_ref=closed.case_ref or "",
            expected_version=closed.case_version or 0,
            waiting_on=WaitingOn.DEV,
        ),
        actor(desk_context, "consult_a"),
    )
    reopened = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "consult_a"))
    assert reopened.lifecycle_status is LifecycleStatus.OPEN
    assert reopened.status is CaseStatus.IN_PROGRESS
    assert reopened.waiting_on is WaitingOn.DEV


def test_partial_delivery_retry_does_not_repeat_successful_items(desk_context: DeskContext) -> None:
    created = create_case(
        desk_context,
        parts=(TextPart("先发文字"), ImagePart(desk_context.media_id), TextPart("后发文字")),
    )
    with desk_context.session_factory() as session:
        delivery = session.scalar(select(Delivery).where(Delivery.id == created.delivery_ids[0]))
        assert delivery is not None
        items = session.scalars(
            select(DeliveryItem)
            .where(DeliveryItem.delivery_id == delivery.id)
            .order_by(DeliveryItem.position)
        ).all()
        image_req_id = items[1].req_id
    adapter = FakeWeComAdapter()
    adapter.fail_next(image_req_id)
    first_try = asyncio.run(DeliveryWorker(desk_context.desk, adapter).deliver_pending())
    assert first_try.sent_items == 1
    assert first_try.failed_bundles == 1

    with desk_context.session_factory() as session, session.begin():
        delivery = session.get(Delivery, created.delivery_ids[0])
        assert delivery is not None
        delivery.next_attempt_at -= timedelta(minutes=5)
    deliver(desk_context, adapter)
    assert len(adapter.sent) == 2
    assert [message.req_id for message in adapter.sent].count(adapter.sent[0].req_id) == 1
    final = desk_context.desk.get_case(created.case_ref or "", actor(desk_context, "consult_a"))
    assert final.waiting_on is WaitingOn.DEV


def test_correction_is_append_only_and_copies_the_formal_message(desk_context: DeskContext) -> None:
    source = create_case(desk_context, source_msgid="source-message")
    target = desk_context.desk.execute(
        CreateCase(
            title="另一个问题",
            consult_queue_id=desk_context.teams["consult"],
            developer_id=desk_context.users["dev_c"],
            parts=(TextPart("正确事件的初始内容"),),
            source_msgid="target-message",
        ),
        actor(desk_context, "consult_b"),
    )
    source_view = desk_context.desk.get_case(
        source.case_ref or "", actor(desk_context, "consult_a")
    )
    target_view = desk_context.desk.get_case(
        target.case_ref or "", actor(desk_context, "consult_a")
    )
    desk_context.desk.execute(
        CorrectEntry(
            case_ref=source.case_ref or "",
            expected_version=source_view.version,
            entry_id=source_view.entries[0].id,
            target_case_ref=target.case_ref or "",
            target_expected_version=target_view.version,
        ),
        actor(desk_context, "consult_a"),
    )
    corrected_source = desk_context.desk.get_case(
        source.case_ref or "", actor(desk_context, "consult_a")
    )
    corrected_target = desk_context.desk.get_case(
        target.case_ref or "", actor(desk_context, "consult_a")
    )
    assert corrected_source.entries[-1].kind is CaseEntryKind.CORRECTION
    assert corrected_target.entries[-1].kind is CaseEntryKind.CORRECTION
    assert corrected_target.entries[-1].corrects_entry_id == source_view.entries[0].id
    assert corrected_target.entries[-1].parts[0].text == "客户无法登录"
    assert corrected_target.entries[-1].message_intent is MessageIntent.HANDOFF
