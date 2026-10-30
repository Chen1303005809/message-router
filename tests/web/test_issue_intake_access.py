from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from kefu.config import Settings
from kefu.media.storage import InMemoryObjectStorage
from kefu.persistence.models import WeComChannel
from kefu.web.app import create_app
from tests.conftest import DeskContext


def _settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite://",
        object_storage_backend="local",
        object_storage_root=Path("/private/tmp/kefu-issue-intake-access"),
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


def _client(desk_context: DeskContext) -> TestClient:
    return TestClient(
        create_app(
            session_factory=desk_context.session_factory,
            settings=_settings(),
            storage=InMemoryObjectStorage(),
        )
    )


def _bind_consult_queue(desk_context: DeskContext) -> None:
    with desk_context.session_factory() as session, session.begin():
        channel = session.scalar(
            select(WeComChannel).where(WeComChannel.team_id == desk_context.teams["consult"])
        )
        assert channel is not None
        channel.chatid = "chat-consult"
        channel.initialized_at = datetime.now(UTC)
        channel.active = True


def test_session_and_page_permissions_are_exposed_as_json(desk_context: DeskContext) -> None:
    client = _client(desk_context)
    anonymous = client.get("/api/session")
    assert anonymous.status_code == 200
    assert anonymous.json()["authenticated"] is False

    consultant = client.get("/api/session", headers={"X-WeCom-UserId": "consult-a"})
    assert consultant.status_code == 200
    assert consultant.json()["authenticated"] is True
    assert consultant.json()["display_name"] == "咨询甲"
    assert consultant.json()["is_global_admin"] is False

    forbidden = client.get(
        "/api/admin/overview",
        headers={"X-WeCom-UserId": "outsider"},
    )
    assert forbidden.status_code == 403
    assert forbidden.headers["content-type"].startswith("application/json")
    assert forbidden.json()["detail"] == "只有问题中心总管理员可以管理组织和授权"


def test_intake_options_and_draft_creation_enforce_consult_queue_access(
    desk_context: DeskContext,
) -> None:
    _bind_consult_queue(desk_context)
    client = _client(desk_context)
    options = client.get("/api/intake/options", headers={"X-WeCom-UserId": "consult-a"})
    assert options.status_code == 200
    assert options.json()["consult_queues"][0]["id"] == str(desk_context.teams["consult"])

    anonymous = client.post(
        "/api/issues/drafts",
        data={
            "pasted_text": "需要验证登录",
            "consult_queue_id": str(desk_context.teams["consult"]),
        },
    )
    assert anonymous.status_code == 401

    forbidden = client.post(
        "/api/issues/drafts",
        headers={"X-WeCom-UserId": "outsider"},
        data={
            "pasted_text": "无权限创建的问题",
            "consult_queue_id": str(desk_context.teams["consult"]),
        },
    )
    assert forbidden.status_code == 403
    assert forbidden.headers["content-type"].startswith("application/json")


def test_unroutable_dev_team_is_reported_by_intake_options_api(
    desk_context: DeskContext,
) -> None:
    _bind_consult_queue(desk_context)
    with desk_context.session_factory() as session, session.begin():
        session.execute(
            update(WeComChannel)
            .where(
                WeComChannel.team_id.in_([desk_context.teams["dev_a"], desk_context.teams["dev_b"]])
            )
            .values(active=False)
        )

    response = _client(desk_context).get(
        "/api/intake/options",
        headers={"X-WeCom-UserId": "consult-a"},
    )
    assert response.status_code == 200
    assert response.json()["consult_queues"]
    assert response.json()["dev_teams"] == []
