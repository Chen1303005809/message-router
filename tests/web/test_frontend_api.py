from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from kefu.case_desk.contracts import Actor, CreateCase, TextPart
from kefu.config import Settings
from kefu.persistence.models import Case, CaseStatus, WaitingOn, WeComChannel
from kefu.web.app import create_app
from tests.conftest import DeskContext


def test_issue_api_is_read_only_and_accept_is_explicit_and_idempotent(
    desk_context: DeskContext, tmp_path: Path
) -> None:
    _ensure_consult_channel(desk_context)
    created = desk_context.desk.execute(
        CreateCase(
            title="API 受理回归",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            suspected_type="盘中",
            parts=(TextPart("先读取，再由前端发起受理"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    client = TestClient(
        create_app(
            session_factory=desk_context.session_factory,
            settings=_settings(tmp_path),
        )
    )
    headers = {"X-WeCom-UserId": "dev-a"}

    detail = client.get(f"/api/issues/{created.case_ref}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == CaseStatus.PENDING_CONFIRMATION.value
    assert "waiting_on" not in detail.json()
    assert all("waiting_on" not in entry["metadata"] for entry in detail.json()["entries"])

    accepted = client.post(
        f"/api/issues/{created.case_ref}/accept",
        headers=headers,
        json={"expected_version": detail.json()["version"]},
    )
    assert accepted.status_code == 200
    assert accepted.json()["accepted"] is True

    repeated = client.post(
        f"/api/issues/{created.case_ref}/accept",
        headers=headers,
        json={"expected_version": detail.json()["version"]},
    )
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    assert repeated.json()["accepted"] is False


def test_reopen_api_always_routes_to_development(
    desk_context: DeskContext, tmp_path: Path
) -> None:
    _ensure_consult_channel(desk_context)
    created = desk_context.desk.execute(
        CreateCase(
            title="API 重开默认路由回归",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            suspected_type="盘中",
            parts=(TextPart("关闭后重开应进入研发路由"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    client = TestClient(
        create_app(
            session_factory=desk_context.session_factory,
            settings=_settings(tmp_path),
        )
    )
    headers = {"X-WeCom-UserId": "consult-a"}
    detail = client.get(f"/api/issues/{created.case_ref}", headers=headers).json()
    closed = client.post(
        f"/api/issues/{created.case_ref}/close",
        headers=headers,
        json={"expected_version": detail["version"]},
    )
    assert closed.status_code == 200

    reopened = client.post(
        f"/api/issues/{created.case_ref}/reopen",
        headers=headers,
        json={"expected_version": closed.json()["version"]},
    )
    assert reopened.status_code == 200
    with desk_context.session_factory() as session:
        issue = session.scalar(select(Case).where(Case.case_ref == created.case_ref))
        assert issue is not None
        assert issue.waiting_on is WaitingOn.DEV


def test_openapi_exposes_typed_api_without_frontend_waiting_on_field(
    desk_context: DeskContext, tmp_path: Path
) -> None:
    app = create_app(
        session_factory=desk_context.session_factory,
        settings=_settings(tmp_path),
    )
    document = app.openapi()
    schemas = document["components"]["schemas"]

    assert "/api/session" in document["paths"]
    assert "/api/issues/{issue_number}/accept" in document["paths"]
    assert "/api/admin/teams/{team_id}/channel" in document["paths"]
    assert "waiting_on" not in schemas["IssueListItem"]["properties"]
    assert "waiting_on" not in schemas["IssueDetailResponse"]["properties"]


def _settings(media_root: Path) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite://",
        object_storage_backend="local",
        object_storage_root=media_root,
        s3_endpoint_url=None,
        s3_bucket="kefu-media",
        s3_access_key_id=None,
        s3_secret_access_key=None,
        wecom_transport="fake",
        auth_mode="development",
        web_base_url="http://testserver",
        wecom_corp_id=None,
        wecom_agent_id=None,
        wecom_corp_secret=None,
        dev_wecom_userid=None,
        session_cookie_secure=False,
    )


def _ensure_consult_channel(context: DeskContext) -> None:
    with context.session_factory() as session, session.begin():
        channel = session.scalar(
            select(WeComChannel).where(WeComChannel.team_id == context.teams["consult"])
        )
        assert channel is not None
        channel.chatid = "chat-consult-api"
