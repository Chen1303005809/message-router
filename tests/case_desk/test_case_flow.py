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
    TextPart,
    TransferConsultant,
    TransferDeveloper,
    TransferDevTeam,
)
from kefu.case_desk.errors import Conflict, Forbidden
from kefu.persistence.models import (
    CaseEntryKind,
    Delivery,
    DeliveryItem,
    DeliveryStatus,
    LifecycleStatus,
    MessageIntent,
    PartKind,
    WaitingOn,
)
from kefu.relay.delivery import DeliveryWorker
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
        PartKind.TEXT,
    ]
    marker = f"〔KF·{result.case_ref}〕"
    content = adapter.sent[0].payload["content"]
    assert content == (
        f"<@dev-a>\n事件标题：登录失败\n发言人：咨询甲（咨询侧）\n\n"
        f"第一段文字\n第二段文字\n\n{marker}"
    )
    assert str(content).count(marker) == 1
    card = adapter.sent[-1].payload["template_card"]
    assert card["main_title"]["title"] == "登录失败"
    assert card["main_title"]["desc"] == "发言人：咨询甲"
    assert marker in card["sub_title_text"]
    assert card["jump_list"] == [
        {
            "type": 1,
            "title": "查看事件历史",
            "url": f"http://localhost:8000/events/{result.case_ref}",
        }
    ]


def test_developer_can_reply_without_becoming_current_handler(desk_context: DeskContext) -> None:
    created = create_case(desk_context)
    initial_adapter = deliver(desk_context)
    initial_content = initial_adapter.sent[0].payload["content"]
    assert str(initial_content).startswith("<@dev-a>\n")
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
        f"事件标题：登录失败\n发言人：研发乙（研发侧）\n\n"
        f"已定位到权限配置\n\n〔KF·{created.case_ref}〕"
    )
    assert "<@" not in str(response_content)


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
    assert closed.waiting_on is WaitingOn.NONE


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


def test_image_first_bundle_still_uses_one_event_card(desk_context: DeskContext) -> None:
    created = create_case(
        desk_context,
        parts=(ImagePart(desk_context.media_id), TextPart("图片后的补充说明")),
    )
    adapter = deliver(desk_context)
    assert [message.kind for message in adapter.sent] == [
        PartKind.TEXT,
        PartKind.IMAGE,
        PartKind.TEXT,
    ]
    content = adapter.sent[0].payload["content"]
    assert content == (
        f"<@dev-a>\n事件标题：登录失败\n发言人：咨询甲（咨询侧）\n\n"
        f"图片后的补充说明\n\n〔KF·{created.case_ref}〕"
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
    assert len(adapter.sent) == 3
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
