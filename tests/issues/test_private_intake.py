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
from kefu.persistence.models import (
    Case,
    MembershipRole,
    PrivateIntakeHandoff,
    TeamMembership,
    WeComChannel,
)
from kefu.web.app import create_app
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
    second_media_id = MediaIngestor(desk_context.session_factory, storage).ingest(
        InboundImagePart(b"second private screenshot", "image/png")
    )
    issues = _management(desk_context, storage=storage)
    actor = Actor(desk_context.users["consult_a"])
    token = issues.create_private_intake_handoff(
        msgid="private-intake-msg-2",
        actor_id=actor.user_id,
        raw_text="请检查 <查询页面> 的超时问题",
        parts=(TextPart("请检查 <查询页面> 的超时问题"), ImagePart(media_id)),
    )
    second_token = issues.create_private_intake_handoff(
        msgid="private-intake-msg-2b",
        actor_id=actor.user_id,
        raw_text="第二条私聊交接内容",
        parts=(TextPart("第二条私聊交接内容"), ImagePart(second_media_id)),
    )
    expired_token = issues.create_private_intake_handoff(
        msgid="private-intake-msg-expired",
        actor_id=actor.user_id,
        raw_text="已过期的私聊交接内容",
        parts=(TextPart("已过期的私聊交接内容"),),
    )
    with desk_context.session_factory() as session, session.begin():
        expired_handoff = session.scalar(
            select(PrivateIntakeHandoff).where(PrivateIntakeHandoff.token == expired_token)
        )
        assert expired_handoff is not None
        expired_handoff.expires_at = datetime.now(UTC) - timedelta(seconds=1)
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
        assert 'src="static/private-intake.js"' in landing.text
        assert token not in landing.text
        handoff_script = client.get("/static/private-intake.js")
        assert handoff_script.status_code == 200
        assert "location.hash.slice(1)" in handoff_script.text
        assert 'sessionStorage.setItem(key, token)' in handoff_script.text
        assert 'navigate("/issues/new?private_intake=1")' in handoff_script.text
        assert "sessionStorage.removeItem(key)" in handoff_script.text
        continue_flow = handoff_script.text.split("} else {", maxsplit=1)[1]
        assert "sessionStorage.removeItem(key)" not in continue_flow
        app_script = client.get("/static/app.js")
        assert app_script.status_code == 200
        assert "privateIntakeMode" in app_script.text
        assert '"X-Private-Intake-Token"' in app_script.text
        assert "URL.createObjectURL(blob)" in app_script.text

        continuation = client.get(
            "/issues/private-intake/continue",
            headers={"X-WeCom-UserId": "consult-a"},
        )
        assert continuation.status_code == 200
        assert 'src="static/private-intake.js"' in continuation.text
        assert token not in continuation.text

        opened = client.post(
            "/api/issues/private-intake/open",
            json={"token": token},
            headers={"X-WeCom-UserId": "consult-a"},
        )
        assert opened.status_code == 200
        assert opened.headers.get("set-cookie") is None
        second_opened = client.post(
            "/api/issues/private-intake/open",
            json={"token": second_token},
            headers={"X-WeCom-UserId": "consult-a"},
        )
        assert second_opened.status_code == 200
        assert second_opened.headers.get("set-cookie") is None

        page = client.get(
            "/issues/new?private_intake=1",
            headers={"X-WeCom-UserId": "consult-a"},
        )
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store, private"
        assert page.headers["referrer-policy"] == "no-referrer"
        assert "请检查 &lt;查询页面&gt; 的超时问题" not in page.text
        assert token not in page.text

        ordinary_page = client.get("/issues/new", headers={"X-WeCom-UserId": "consult-a"})
        assert ordinary_page.status_code == 200
        intake = client.get(
            "/api/issues/intake-options",
            headers={"X-WeCom-UserId": "consult-a"},
        )
        assert intake.status_code == 200
        assert intake.json()["private_intake"] is None

        missing_token = client.get(
            "/api/issues/intake-options?private_intake=1",
            headers={"X-WeCom-UserId": "consult-a"},
        )
        assert missing_token.status_code == 422

        first_intake = client.get(
            "/api/issues/intake-options?private_intake=1",
            headers={
                "X-WeCom-UserId": "consult-a",
                "X-Private-Intake-Token": token,
            },
        )
        assert first_intake.status_code == 200
        assert first_intake.headers["cache-control"] == "no-store, private"
        private_intake = first_intake.json()["private_intake"]
        assert private_intake["raw_text"] == "请检查 <查询页面> 的超时问题"
        media_path = private_intake["images"][0]["url"]
        assert media_path == f"/issues/private-intakes/media/{media_id}"

        second_intake = client.get(
            "/api/issues/intake-options?private_intake=1",
            headers={
                "X-WeCom-UserId": "consult-a",
                "X-Private-Intake-Token": second_token,
            },
        )
        assert second_intake.status_code == 200
        assert second_intake.json()["private_intake"]["raw_text"] == "第二条私聊交接内容"
        second_media_path = second_intake.json()["private_intake"]["images"][0]["url"]
        assert second_media_path == f"/issues/private-intakes/media/{second_media_id}"

        no_marker = client.get(
            "/api/issues/intake-options",
            headers={
                "X-WeCom-UserId": "consult-a",
                "X-Private-Intake-Token": token,
            },
        )
        assert no_marker.status_code == 200
        assert no_marker.json()["private_intake"] is None

        image = client.get(
            media_path,
            headers={
                "X-WeCom-UserId": "consult-a",
                "X-Private-Intake-Token": token,
            },
        )
        assert image.status_code == 200
        assert image.content == b"private screenshot"
        assert image.headers["cache-control"] == "no-store, private"
        wrong_image_token = client.get(
            second_media_path,
            headers={
                "X-WeCom-UserId": "consult-a",
                "X-Private-Intake-Token": token,
            },
        )
        assert wrong_image_token.status_code == 404
        second_image = client.get(
            second_media_path,
            headers={
                "X-WeCom-UserId": "consult-a",
                "X-Private-Intake-Token": second_token,
            },
        )
        assert second_image.status_code == 200
        assert second_image.content == b"second private screenshot"

        denied_intake = client.get(
            "/api/issues/intake-options?private_intake=1",
            headers={
                "X-WeCom-UserId": "dev-a",
                "X-Private-Intake-Token": token,
            },
        )
        assert denied_intake.status_code == 403
        expired_intake = client.get(
            "/api/issues/intake-options?private_intake=1",
            headers={
                "X-WeCom-UserId": "consult-a",
                "X-Private-Intake-Token": expired_token,
            },
        )
        assert expired_intake.status_code == 404
        assert "agentConfig" not in page.text


def test_private_handoff_token_is_scoped_to_explicit_draft_submission(
    desk_context, monkeypatch
):
    import kefu.web.app as web_app

    class StubAgent:
        async def draft_issue(
            self,
            pasted_text: str,
            acceptance_groups: list[str],
            *,
            request_id: str | None = None,
            source_group_name: str | None = None,
        ):
            return {
                "issue_title": "交接隔离测试",
                "issue_description": f"{pasted_text}的整理结果",
                "initial_acceptance_group": acceptance_groups[0],
                "suspected_type": "盘中",
                "current_situation": "等待研发确认",
            }

        async def close(self):
            return None

    monkeypatch.setattr(web_app, "Agent", lambda _settings, _tools: StubAgent())
    storage = InMemoryObjectStorage()
    first_media_id = MediaIngestor(desk_context.session_factory, storage).ingest(
        InboundImagePart(b"must stay with the private handoff", "image/png")
    )
    issues = _management(desk_context, storage=storage)
    actor = Actor(desk_context.users["consult_a"])
    first_token = issues.create_private_intake_handoff(
        msgid="private-intake-submit-1",
        actor_id=actor.user_id,
        raw_text="第一标签页私聊内容[图片]",
        parts=(TextPart("第一标签页私聊内容"), ImagePart(first_media_id)),
    )
    second_token = issues.create_private_intake_handoff(
        msgid="private-intake-submit-2",
        actor_id=actor.user_id,
        raw_text="第二标签页私聊内容",
        parts=(TextPart("第二标签页私聊内容"),),
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
    user_headers = {"X-WeCom-UserId": "consult-a"}
    queue_id = str(desk_context.teams["consult"])

    with TestClient(app) as client:
        normal_response = client.post(
            "/api/issues/drafts",
            headers={
                **user_headers,
                "X-Private-Intake-Token": first_token,
            },
            data={
                "consult_queue_id": queue_id,
                "pasted_text": "普通入口内容",
            },
        )
        assert normal_response.status_code == 201, normal_response.text
        normal_draft_id = normal_response.json()["draft_id"]
        normal_draft = client.get(
            f"/api/issues/drafts/{normal_draft_id}",
            headers=user_headers,
        )
        assert normal_draft.status_code == 200
        assert normal_draft.json()["fields"]["issue_source"] == "工作台"
        assert not any(part["kind"] == "image" for part in normal_draft.json()["parts"])
        assert issues.get_private_intake_handoff(first_token, actor).raw_text == (
            "第一标签页私聊内容[图片]"
        )

        opened = client.post(
            "/api/issues/private-intake/open",
            headers=user_headers,
            json={"token": second_token},
        )
        assert opened.status_code == 200
        assert opened.headers.get("set-cookie") is None

        private_response = client.post(
            "/api/issues/drafts?private_intake=1",
            headers={
                **user_headers,
                "X-Private-Intake-Token": second_token,
            },
            data={
                "consult_queue_id": queue_id,
                "pasted_text": "第二标签页私聊内容",
            },
        )
        assert private_response.status_code == 201, private_response.text
        private_draft_id = private_response.json()["draft_id"]
        private_draft = client.get(
            f"/api/issues/drafts/{private_draft_id}",
            headers=user_headers,
        )
        assert private_draft.status_code == 200
        assert private_draft.json()["fields"]["issue_source"] == "企业微信私聊"
        with pytest.raises(Conflict):
            issues.get_private_intake_handoff(second_token, actor)

        repeated_private_response = client.post(
            "/api/issues/drafts?private_intake=1",
            headers={
                **user_headers,
                "X-Private-Intake-Token": second_token,
            },
            data={
                "consult_queue_id": queue_id,
                "pasted_text": "重复提交不得覆盖内容",
            },
        )
        assert repeated_private_response.status_code == 200
        assert repeated_private_response.json()["draft_id"] == private_draft_id
