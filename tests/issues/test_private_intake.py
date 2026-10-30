from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kefu.case_desk.contracts import Actor, ImagePart, TextPart
from kefu.case_desk.errors import Conflict, Forbidden
from kefu.config import Settings
from kefu.issues.contracts import IssueFields, IssueIntake
from kefu.issues.service import IssueManagement
from kefu.media.storage import InMemoryObjectStorage, MediaIngestor
from kefu.persistence.models import Case, MembershipRole, TeamMembership, WeComChannel
from kefu.web.app import _PRIVATE_INTAKE_COOKIE, create_app
from kefu.wecom.transport import InboundImagePart


def _management(desk_context, *, storage=None):
    now = datetime.now(UTC)
    with desk_context.session_factory() as session, session.begin():
        session.add(
            WeComChannel(
                team_id=desk_context.teams["consult"],
                chatid="chat-consult",
                initialized_at=now,
            )
        )
    return IssueManagement(
        desk_context.session_factory,
        desk_context.desk,
        Settings.from_env(),
        storage=storage,
    )


def _intake(desk_context, raw_text: str) -> IssueIntake:
    return IssueIntake(
        consult_queue_id=desk_context.teams["consult"],
        raw_text=raw_text,
        parts=(TextPart(raw_text),),
        fields=IssueFields(
            issue_title="私聊问题",
            issue_description=raw_text,
            initial_acceptance_team_id=desk_context.teams["dev_a"],
            suspected_type="盘中",
            deadline=datetime.now(UTC) + timedelta(hours=18),
            issue_source="企业微信私聊",
        ),
    )


def test_private_handoff_is_owned_one_time_and_confirmation_is_idempotent(desk_context):
    issues = _management(desk_context)
    actor = Actor(desk_context.users["consult_a"])
    token = issues.create_private_intake_handoff(
        msgid="private-intake-msg-1",
        actor_id=actor.user_id,
        raw_text="私聊里的问题描述",
        parts=(TextPart("私聊里的问题描述"),),
    )

    handoff = issues.get_private_intake_handoff(token, actor)
    assert handoff.raw_text == "私聊里的问题描述"
    with pytest.raises(Forbidden):
        issues.get_private_intake_handoff(
            token,
            Actor(desk_context.users["outsider"]),
        )

    draft = issues.create_intake_draft(
        _intake(desk_context, handoff.raw_text),
        actor,
        private_handoff_token=token,
    )
    repeated_draft = issues.create_intake_draft(
        _intake(desk_context, "重复提交不得覆盖内容"),
        actor,
        private_handoff_token=token,
    )
    assert repeated_draft.id == draft.id
    with pytest.raises(Conflict):
        issues.get_private_intake_handoff(token, actor)

    first_confirmation = issues.confirm_intake(draft.id, draft.fields, actor)
    second_confirmation = issues.confirm_intake(draft.id, draft.fields, actor)
    assert first_confirmation.issue_number == second_confirmation.issue_number
    with desk_context.session_factory() as session:
        case = session.scalar(select(Case).where(Case.case_ref == draft.issue_number))
        assert case is not None
        assert case.creator_id == actor.user_id
        assert case.current_consultant_id == actor.user_id
        assert case.issue_source == "企业微信私聊"


def test_private_handoff_h5_prefills_only_for_owner(desk_context):
    storage = InMemoryObjectStorage()
    media_id = MediaIngestor(desk_context.session_factory, storage).ingest(
        InboundImagePart(b"private screenshot", "image/png")
    )
    issues = _management(desk_context, storage=storage)
    actor = Actor(desk_context.users["consult_a"])
    token = issues.create_private_intake_handoff(
        msgid="private-intake-msg-2",
        actor_id=actor.user_id,
        raw_text="请检查 <查询页面> 的超时问题",
        parts=(TextPart("请检查 <查询页面> 的超时问题"), ImagePart(media_id)),
    )
    with desk_context.session_factory() as session, session.begin():
        session.add(
            TeamMembership(
                team_id=desk_context.teams["dev_a"],
                user_id=actor.user_id,
                role=MembershipRole.MEMBER,
                valid_from=datetime.now(UTC),
            )
        )
    settings = replace(
        Settings.from_env(),
        auth_mode="development",
        dev_wecom_userid=None,
        web_base_url="http://testserver",
    )
    app = create_app(
        session_factory=desk_context.session_factory,
        settings=settings,
        storage=storage,
    )
    with TestClient(app) as client:
        landing = client.get("/issues/private-intake")
        assert landing.status_code == 200
        assert "location.hash.slice(1)" in landing.text
        assert token not in landing.text

        continuation = client.get(
            "/issues/private-intake/continue",
            headers={"X-WeCom-UserId": "consult-a"},
        )
        assert continuation.status_code == 200
        assert "/api/issues/private-intake/open" in continuation.text
        assert token not in continuation.text

        opened = client.post(
            "/api/issues/private-intake/open",
            json={"token": token},
            headers={"X-WeCom-UserId": "consult-a"},
        )
        assert opened.status_code == 200
        assert _PRIVATE_INTAKE_COOKIE in opened.headers["set-cookie"]

        page = client.get("/issues/new", headers={"X-WeCom-UserId": "consult-a"})
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store, private"
        assert page.headers["referrer-policy"] == "no-referrer"
        assert "请检查 &lt;查询页面&gt; 的超时问题" in page.text
        assert token not in page.text
        media_path = f"/issues/private-intakes/media/{media_id}"
        assert media_path in page.text
        image = client.get(media_path, headers={"X-WeCom-UserId": "consult-a"})
        assert image.status_code == 200
        assert image.content == b"private screenshot"
        assert image.headers["cache-control"] == "no-store, private"
        assert "agentConfig" not in page.text

        denied = client.get("/issues/new", headers={"X-WeCom-UserId": "dev-a"})
        assert denied.status_code == 403
