from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import update

from kefu.config import Settings
from kefu.media.storage import InMemoryObjectStorage
from kefu.persistence.models import User, WeComChannel
from kefu.web.app import create_app
from tests.conftest import DeskContext


def _settings(*, auth_mode: str = "development") -> Settings:
    oauth_enabled = auth_mode == "wecom_oauth"
    return Settings(
        database_url="sqlite+pysqlite://",
        object_storage_backend="local",
        object_storage_root=Path("/private/tmp/kefu-issue-intake-access"),
        s3_endpoint_url=None,
        s3_bucket="kefu-media",
        s3_access_key_id=None,
        s3_secret_access_key=None,
        wecom_transport="fake",
        auth_mode=auth_mode,
        web_base_url="https://events.example.test",
        wecom_corp_id="corp-id" if oauth_enabled else None,
        wecom_agent_id="1000001" if oauth_enabled else None,
        wecom_corp_secret="corp-secret" if oauth_enabled else None,
        dev_wecom_userid=None,
        session_cookie_secure=False,
    )


def _client(desk_context: DeskContext, *, auth_mode: str = "development") -> TestClient:
    return TestClient(
        create_app(
            session_factory=desk_context.session_factory,
            settings=_settings(auth_mode=auth_mode),
            storage=InMemoryObjectStorage(),
        )
    )


def _bind_consult_queue(desk_context: DeskContext) -> None:
    with desk_context.session_factory() as session, session.begin():
        session.add(
            WeComChannel(
                team_id=desk_context.teams["consult"],
                chatid="chat-consult",
                initialized_at=datetime.now(UTC),
                active=True,
            )
        )


def test_consultant_keeps_new_issue_as_default_landing_page(desk_context: DeskContext) -> None:
    _bind_consult_queue(desk_context)
    client = _client(desk_context)

    response = client.get(
        "/",
        headers={"X-WeCom-UserId": "consult-a"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "新建问题" in response.text
    assert 'data-flash-notice="true"' not in response.text


def test_user_without_consult_queue_is_redirected_to_issue_list_with_notice(
    desk_context: DeskContext,
) -> None:
    client = _client(desk_context)
    headers = {"X-WeCom-UserId": "outsider"}

    blocked = client.get("/issues/new", headers=headers, follow_redirects=False)

    assert blocked.status_code == 302
    location = urlsplit(blocked.headers["location"])
    assert location.path == "/issues"
    assert parse_qs(location.query) == {"notice": ["missing_consult_queue"]}

    listing = client.get(blocked.headers["location"], headers=headers)
    assert listing.status_code == 200
    assert "问题中心" in listing.text
    assert "没有可用的咨询队列" in listing.text
    assert 'data-flash-notice="true"' in listing.text
    assert 'data-flash-notice-close="true"' in listing.text
    assert "setTimeout(() => notice.remove(), 5000)" in listing.text

    injected_notice = client.get(
        "/issues?notice=%3Cscript%3Ealert(1)%3C%2Fscript%3E",
        headers=headers,
    )
    assert injected_notice.status_code == 200
    assert "<script>alert(1)</script>" not in injected_notice.text
    assert 'data-flash-notice="true"' not in injected_notice.text


def test_global_admin_without_consult_queue_can_reach_authorization_page(
    desk_context: DeskContext,
) -> None:
    with desk_context.session_factory() as session, session.begin():
        user = session.get(User, desk_context.users["outsider"])
        assert user is not None
        user.is_global_admin = True

    client = _client(desk_context)
    headers = {"X-WeCom-UserId": "outsider"}
    listing = client.get("/issues/new", headers=headers, follow_redirects=True)

    assert listing.status_code == 200
    assert "没有可用的咨询队列" in listing.text
    assert 'href="/admin"' in listing.text
    assert client.get("/admin", headers=headers).status_code == 200


def test_user_without_routable_dev_team_is_redirected_with_specific_notice(
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

    client = _client(desk_context)
    blocked = client.get(
        "/issues/new",
        headers={"X-WeCom-UserId": "consult-a"},
        follow_redirects=False,
    )

    assert blocked.status_code == 302
    assert parse_qs(urlsplit(blocked.headers["location"]).query) == {"notice": ["missing_dev_team"]}
    listing = client.get(
        blocked.headers["location"],
        headers={"X-WeCom-UserId": "consult-a"},
    )
    assert listing.status_code == 200
    assert "没有已绑定群聊的研发受理组" in listing.text


def test_login_and_issue_creation_authorization_are_unchanged(
    desk_context: DeskContext,
) -> None:
    oauth_client = _client(desk_context, auth_mode="wecom_oauth")
    login = oauth_client.get("/issues/new", follow_redirects=False)
    assert login.status_code == 302
    assert "open.weixin.qq.com" in login.headers["location"]

    client = _client(desk_context)
    forbidden = client.post(
        "/issues/draft",
        headers={"X-WeCom-UserId": "outsider"},
        data={
            "pasted_text": "无权限创建的问题",
            "consult_queue_id": str(desk_context.teams["consult"]),
        },
        follow_redirects=False,
    )
    assert forbidden.status_code == 403

    regular_user_admin_page = client.get(
        "/admin",
        headers={"X-WeCom-UserId": "outsider"},
    )
    assert regular_user_admin_page.status_code == 403
