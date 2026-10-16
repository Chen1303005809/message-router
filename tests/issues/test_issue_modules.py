from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from kefu.agent.service import Agent, _jsonable, build_tool_registry
from kefu.case_desk.contracts import Actor, ImagePart, TextPart
from kefu.case_desk.errors import Forbidden, NotFound
from kefu.case_desk.service import CaseDesk
from kefu.config import Settings
from kefu.inspection.service import IssueInspectionService
from kefu.issues.contracts import IssueFields, IssueIntake
from kefu.issues.service import IssueManagement, map_image_placeholders
from kefu.media.storage import InMemoryObjectStorage, MediaIngestor
from kefu.messages.push import MessagePush, MessagePushWorker
from kefu.messages.receiver import MessageReceiver
from kefu.messages.recipients import RecipientKind, RecipientReference, RecipientResolver
from kefu.persistence.models import (
    Base,
    Case,
    CaseEntry,
    CaseEntryPart,
    Delivery,
    DeliveryDestination,
    DeliveryStatus,
    IssueInspectionState,
    LifecycleStatus,
    MembershipRole,
    MessageOutbox,
    Team,
    TeamKind,
    TeamMembership,
    User,
    WaitingOn,
    WeComChannel,
)
from kefu.relay.handler import Relay
from kefu.wecom.transport import FakeWeComAdapter, InboundImagePart


@pytest.fixture
def issue_context():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    actor_id = uuid4()
    queue_id = uuid4()
    acceptance_id = uuid4()
    user_id = uuid4()
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        session.add_all(
            [
                User(id=actor_id, wecom_userid="consult-1", display_name="咨询人员"),
                User(id=user_id, wecom_userid="dev-1", display_name="研发人员"),
                Team(id=queue_id, kind=TeamKind.CONSULT_QUEUE, name="咨询队列"),
                Team(id=acceptance_id, kind=TeamKind.DEV, name="研发一组"),
                TeamMembership(
                    team_id=queue_id,
                    user_id=actor_id,
                    role=MembershipRole.MEMBER,
                    valid_from=now,
                ),
                TeamMembership(
                    team_id=acceptance_id,
                    user_id=user_id,
                    role=MembershipRole.MEMBER,
                    valid_from=now,
                ),
                WeComChannel(team_id=queue_id, chatid="chat-consult", initialized_at=now),
                WeComChannel(team_id=acceptance_id, chatid="chat-dev", initialized_at=now),
            ]
        )
    storage = InMemoryObjectStorage()
    settings = replace(
        Settings.from_env(),
        issue_default_expiry_hours=3,
        issue_approaching_window_hours=24,
        issue_inspection_interval_hours=12,
        agent_base_url="https://agent.example.test/v1",
        agent_api_key="test-key",
        agent_model="test-model",
    )
    desk = CaseDesk(factory)
    management = IssueManagement(factory, desk, settings, storage=storage)
    push = MessagePush(factory)
    receiver = MessageReceiver(Relay.__new__(Relay), push)
    recipients = RecipientResolver(factory)
    inspection = IssueInspectionService(factory, receiver, recipients, settings)
    try:
        yield {
            "factory": factory,
            "actor": Actor(actor_id),
            "queue_id": queue_id,
            "acceptance_id": acceptance_id,
            "user_id": user_id,
            "settings": settings,
            "storage": storage,
            "management": management,
            "push": push,
            "receiver": receiver,
            "recipients": recipients,
            "inspection": inspection,
        }
    finally:
        engine.dispose()


def test_image_placeholders_map_in_order_and_keep_unmatched_text() -> None:
    first, second, extra = uuid4(), uuid4(), uuid4()

    mapped = map_image_placeholders("开始[图片]中间[图片]结尾[图片]", (first, second))
    assert mapped == (
        TextPart("开始"),
        ImagePart(first),
        TextPart("中间"),
        ImagePart(second),
        TextPart("结尾"),
        TextPart("[图片]"),
    )

    with_extra = map_image_placeholders("开始[图片]结尾", (first, second, extra))
    assert with_extra == (
        TextPart("开始"),
        ImagePart(first),
        TextPart("结尾\n"),
        ImagePart(second),
        ImagePart(extra),
    )


def test_confirm_intake_writes_issue_timeline_and_initial_delivery_idempotently(issue_context):
    factory = issue_context["factory"]
    actor = issue_context["actor"]
    raw_text = "咨询消息：接口超时 [图片] 复现步骤仍可见 [图片]"
    media_id = MediaIngestor(factory, issue_context["storage"]).ingest(
        InboundImagePart(data=b"fake-image", mime_type="image/png")
    )
    parts = map_image_placeholders(raw_text, (media_id,))
    deadline = datetime.now(UTC) + timedelta(hours=18)
    draft = issue_context["management"].create_intake_draft(
        IssueIntake(
            consult_queue_id=issue_context["queue_id"],
            raw_text=raw_text,
            parts=parts,
            fields=IssueFields(
                issue_description="接口请求超时",
                initial_acceptance_team_id=issue_context["acceptance_id"],
                suspected_type="Bug",
                current_situation="登录后偶发超时",
                deadline=deadline,
            ),
        ),
        actor,
    )

    confirmed = issue_context["management"].confirm_intake(draft.id, draft.fields, actor)
    repeated = issue_context["management"].confirm_intake(draft.id, draft.fields, actor)

    with factory() as session:
        issue = session.scalar(select(Case).where(Case.case_ref == confirmed.issue_number))
        assert issue is not None
        assert issue.issue_number == confirmed.issue_number
        assert issue.creator_id == actor.user_id
        assert issue.initial_acceptance_team_id == issue_context["acceptance_id"]
        assert issue.issue_description == "接口请求超时"
        assert issue.suspected_type == "Bug"
        assert issue.issue_current_situation == "登录后偶发超时"
        assert issue.issue_current_status == "问题未回复"
        assert issue.issue_source == "工作台"
        assert issue.bug_number == f"BUG-{confirmed.issue_number}"
        assert issue.initial_expires_at is not None
        stored_deadline = issue.initial_expires_at
        if stored_deadline.tzinfo is None:
            stored_deadline = stored_deadline.replace(tzinfo=UTC)
        assert abs((stored_deadline - deadline).total_seconds()) < 2
        entry_count = session.scalar(
            select(func.count()).select_from(CaseEntry).where(CaseEntry.case_id == issue.id)
        )
        part_count = session.scalar(
            select(func.count())
            .select_from(CaseEntryPart)
            .join(CaseEntry, CaseEntry.id == CaseEntryPart.entry_id)
            .where(CaseEntry.case_id == issue.id)
        )
        deliveries = session.scalars(
            select(Delivery).where(Delivery.entry_id == confirmed.result.entry_id)
        ).all()
        assert entry_count == 1
        assert part_count == 4
        assert len(deliveries) == 1
        assert deliveries[0].status is DeliveryStatus.PENDING
        assert repeated.result.idempotent is True


def test_inspection_sends_periodic_and_one_time_approaching_reminders(issue_context):
    actor = issue_context["actor"]
    deadline = datetime.now(UTC) + timedelta(hours=18)
    draft = issue_context["management"].create_intake_draft(
        IssueIntake(
            consult_queue_id=issue_context["queue_id"],
            raw_text="问题需要排查",
            parts=(TextPart("问题需要排查"),),
            fields=IssueFields(
                issue_description="登录接口失败",
                initial_acceptance_team_id=issue_context["acceptance_id"],
                deadline=deadline,
            ),
        ),
        actor,
    )
    confirmed = issue_context["management"].confirm_intake(draft.id, draft.fields, actor)
    with issue_context["factory"]() as session:
        issue = session.scalar(select(Case).where(Case.case_ref == confirmed.issue_number))
        assert issue is not None
        created_at = issue.created_at.replace(tzinfo=UTC)

    first = issue_context["inspection"].inspect(now=created_at + timedelta(minutes=1))
    periodic = issue_context["inspection"].inspect(now=created_at + timedelta(hours=12))
    repeated = issue_context["inspection"].inspect(now=created_at + timedelta(hours=12))

    assert first.scanned_issues == 1
    assert first.periodic_reminders == 0
    assert first.approaching_reminders == 2
    assert periodic.periodic_reminders == 1
    assert periodic.approaching_reminders == 0
    assert repeated.periodic_reminders == 0
    with issue_context["factory"]() as session:
        messages = session.scalars(select(MessageOutbox)).all()
        assert len(messages) == 3
        assert {message.destination_address for message in messages} == {
            "chat-consult",
            "chat-dev",
        }
        assert session.get(IssueInspectionState, issue.id).approaching_notified_at is not None
        closed_issue = session.get(Case, issue.id)
        assert closed_issue is not None
        closed_issue.lifecycle_status = LifecycleStatus.CLOSED
        closed_issue.waiting_on = WaitingOn.NONE
        session.commit()

    closed = issue_context["inspection"].inspect(now=created_at + timedelta(hours=24))
    assert closed.scanned_issues == 0


def test_person_target_push_is_idempotent_and_retries_transient_failure(issue_context):
    target = issue_context["recipients"].resolve(
        RecipientReference(RecipientKind.PERSON, issue_context["user_id"])
    )
    assert target.destination_type is DeliveryDestination.USER
    first = issue_context["push"].enqueue(
        target, "个人提醒", idempotency_key="person-reminder-1"
    )
    duplicate = issue_context["push"].enqueue(
        target, "个人提醒", idempotency_key="person-reminder-1"
    )
    assert duplicate.id == first.id
    assert duplicate.idempotent is True

    adapter = FakeWeComAdapter()
    adapter.fail_next(f"outbox-{first.id}")
    worker = MessagePushWorker(issue_context["factory"], adapter)
    first_attempt = asyncio.run(worker.deliver_pending())
    assert first_attempt == 0
    with issue_context["factory"]() as session, session.begin():
        message = session.get(MessageOutbox, first.id)
        assert message is not None
        assert message.status is DeliveryStatus.PENDING
        assert message.attempts == 1
        message.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

    assert asyncio.run(worker.deliver_pending()) == 1
    assert len(adapter.sent) == 1
    assert adapter.sent[0].destination_type is DeliveryDestination.USER
    assert adapter.sent[0].destination_address == "dev-1"


def test_agent_tools_reuse_module_interfaces_and_enforce_permissions(issue_context):
    registry = build_tool_registry(
        issue_context["management"],
        issue_context["inspection"],
        issue_context["receiver"],
        issue_context["recipients"],
    )
    schemas = registry.schemas
    assert all(schema["type"] == "function" for schema in schemas)
    assert {schema["function"]["name"] for schema in schemas} >= {
        "issue.create_intake_draft",
        "issue.execute",
        "issue.receive_workbench_message",
        "inspection.run",
        "message.push",
    }

    result = registry.call(
        "message.push",
        {
            "recipient_kind": "consult_queue",
            "recipient_id": str(issue_context["queue_id"]),
            "text": "Agent 群聊提醒",
        },
        issue_context["actor"],
    )
    assert result.queued.idempotent is False
    with issue_context["factory"]() as session:
        queued = session.get(MessageOutbox, result.queued.id)
        assert queued is not None
        assert queued.destination_type is DeliveryDestination.CHAT
        assert queued.destination_address == "chat-consult"

    with pytest.raises(NotFound):
        registry.call(
            "message.push",
            {
                "recipient_kind": "team",
                "recipient_id": str(uuid4()),
                "text": "不能猜测目标",
            },
            issue_context["actor"],
        )
    with pytest.raises(Forbidden):
        registry.call("inspection.run", {}, issue_context["actor"])


def test_agent_issue_execute_uses_issue_management_permissions(issue_context):
    actor = issue_context["actor"]
    draft = issue_context["management"].create_intake_draft(
        IssueIntake(
            consult_queue_id=issue_context["queue_id"],
            raw_text="登录后页面报错",
            parts=(TextPart("登录后页面报错"),),
            fields=IssueFields(
                issue_description="登录后页面报错",
                initial_acceptance_team_id=issue_context["acceptance_id"],
            ),
        ),
        actor,
    )
    issue = issue_context["management"].confirm_intake(draft.id, draft.fields, actor)
    current = issue_context["management"].get(issue.issue_number, actor)
    agent_view = _jsonable(current)
    assert agent_view["issue_number"] == issue.issue_number
    assert "case_ref" not in agent_view
    registry = build_tool_registry(
        issue_context["management"],
        issue_context["inspection"],
        issue_context["receiver"],
        issue_context["recipients"],
    )

    result = registry.call(
        "issue.execute",
        {
            "action": "post_message",
            "issue_number": issue.issue_number,
            "version": current.version,
            "text": "研发已开始排查。",
        },
        actor,
    )

    assert result.issue_number == issue.issue_number
    assert result.entry_id is not None
    with issue_context["factory"]() as session:
        deliveries = session.scalars(
            select(Delivery).where(Delivery.entry_id == result.entry_id)
        ).all()
        assert len(deliveries) == 1
        assert deliveries[0].status is DeliveryStatus.PENDING


def test_agent_uses_openai_chat_completions_with_text_only(issue_context):
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "issue_description": "接口超时",
                                    "initial_acceptance_group": "研发一组",
                                    "suspected_type": "",
                                    "current_situation": "点击保存后无响应",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    agent = Agent(issue_context["settings"], client=client)
    try:
        result = asyncio.run(
            agent.draft_issue("引用消息\n[图片]\n@机器人 保存失败", ["研发一组"])
        )
    finally:
        asyncio.run(agent.close())
        asyncio.run(client.aclose())

    request = captured[0]
    request_body = request.content.decode()
    payload = json.loads(request_body)
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    assert payload["model"] == "test-model"
    assert "[图片]" in payload["messages"][1]["content"]
    assert result["issue_description"] == "接口超时"
    assert "image bytes" not in request_body


def test_workbench_upload_calls_agent_with_text_only_and_confirms_issue(issue_context, monkeypatch):
    import kefu.web.app as web_app

    captured_text: list[str] = []

    class StubAgent:
        async def draft_issue(self, pasted_text: str, acceptance_groups: list[str]):
            captured_text.append(pasted_text)
            assert acceptance_groups == ["研发一组"]
            return {
                "issue_description": "工作台生成的问题描述",
                "initial_acceptance_group": "研发一组",
                "suspected_type": "Bug",
                "current_situation": "上传图片供研发查看",
            }

        async def close(self):
            return None

    stub_agent = StubAgent()
    monkeypatch.setattr(web_app, "Agent", lambda _settings, _tools: stub_agent)

    app = web_app.create_app(
        session_factory=issue_context["factory"],
        settings=issue_context["settings"],
        storage=issue_context["storage"],
    )
    headers = {"X-WeCom-UserId": "consult-1"}
    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        legacy = client.get("/events/new", follow_redirects=False)
        legacy_api = client.get("/api/events/issue-123", follow_redirects=False)
        upload_page = client.get("/issues/new", headers=headers)
        assert root.status_code == 302
        assert root.headers["location"] == "/issues/new"
        assert legacy.status_code == 307
        assert legacy.headers["location"] == "/issues/new"
        assert legacy_api.status_code == 307
        assert legacy_api.headers["location"] == "/api/issues/issue-123"
        assert upload_page.status_code == 200
        assert "上传咨询材料" in upload_page.text

        pasted_text = "引用内容\n[图片]\n@机器人 保存后报错\n[图片]"
        draft_response = client.post(
            "/issues/draft",
            headers=headers,
            data={
                "consult_queue_id": str(issue_context["queue_id"]),
                "pasted_text": pasted_text,
            },
            files=[("images", ("error.png", b"PRIVATE_IMAGE_BYTES", "image/png"))],
            follow_redirects=False,
        )
        assert draft_response.status_code == 303, draft_response.text
        assert captured_text == [pasted_text]
        assert "PRIVATE_IMAGE_BYTES" not in captured_text[0]

        draft_path = draft_response.headers["location"]
        preview = client.get(draft_path, headers=headers)
        assert preview.status_code == 200
        assert "问题状态" in preview.text
        assert "Bug" in preview.text
        assert "[图片]" in preview.text
        assert "<img class=\"intake-image\"" in preview.text
        draft_id = draft_path.rsplit("/", maxsplit=1)[-1]

        local_deadline = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(hours=18)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        confirmation = client.post(
            f"/issues/drafts/{draft_id}/confirm",
            headers=headers,
            data={
                "issue_description": "确认后的问题描述",
                "initial_acceptance_team_id": str(issue_context["acceptance_id"]),
                "suspected_type": "Bug",
                "issue_current_status": "问题未回复",
                "current_situation": "确认后的现状",
                "deadline": local_deadline,
                "bug_number": "",
                "feedback_event_point": "版本上线后首次反馈",
                "issue_source": "工作台",
            },
            follow_redirects=False,
        )
        assert confirmation.status_code == 303
        detail = client.get(confirmation.headers["location"], headers=headers)
        assert detail.status_code == 200
        assert "确认后的问题描述" in detail.text
        assert "版本上线后首次反馈" in detail.text
        issue_number = confirmation.headers["location"].rsplit("/", maxsplit=1)[-1]
        issue_api = client.get(f"/api/issues/{issue_number}", headers=headers)
        assert issue_api.status_code == 200
        assert issue_api.json()["issue_number"] == issue_number
        assert "case_ref" not in issue_api.json()
