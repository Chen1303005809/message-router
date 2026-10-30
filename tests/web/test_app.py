from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from kefu.config import Settings
from kefu.persistence.models import WebSession
from kefu.web.app import create_app
from kefu.web.auth import WebAuthenticator
from tests.conftest import DeskContext


class FakeOAuthClient:
    def authorize_url(self, callback_url: str, state: str) -> str:
        return f"https://oauth.example.test/?callback={callback_url}&state={state}"

    def user_id_for_code(self, code: str) -> str:
        assert code == "one-time-code"
        return "consult-a"


def settings(*, web_base_url: str = "https://issues.example.test") -> Settings:
    return Settings(
        database_url="sqlite+pysqlite://",
        object_storage_backend="local",
        object_storage_root=Path("/private/tmp/kefu-web-auth"),
        s3_endpoint_url=None,
        s3_bucket="kefu-media",
        s3_access_key_id=None,
        s3_secret_access_key=None,
        wecom_transport="fake",
        auth_mode="wecom_oauth",
        web_base_url=web_base_url,
        wecom_corp_id="corp-id",
        wecom_agent_id="1000001",
        wecom_corp_secret="corp-secret",
        dev_wecom_userid=None,
        session_cookie_secure=False,
    )


def _oauth_client(
    desk_context: DeskContext, *, web_base_url: str = "https://issues.example.test"
) -> TestClient:
    configured = settings(web_base_url=web_base_url)
    authenticator = WebAuthenticator(
        desk_context.session_factory,
        configured,
        oauth_client=FakeOAuthClient(),
    )
    return TestClient(
        create_app(
            session_factory=desk_context.session_factory,
            settings=configured,
            authenticator=authenticator,
        )
    )


def test_oauth_callback_creates_server_session_for_the_json_api(
    desk_context: DeskContext,
) -> None:
    client = _oauth_client(desk_context)

    started = client.get("/auth/login?next=%2Fissues", follow_redirects=False)
    assert started.status_code == 302
    login_query = parse_qs(urlparse(started.headers["location"]).query)
    assert login_query["callback"] == ["https://issues.example.test/auth/callback"]
    state = login_query["state"][0]

    completed = client.get(
        f"/auth/callback?code=one-time-code&state={state}",
        follow_redirects=False,
    )
    assert completed.status_code == 302
    assert completed.headers["location"] == "/issues"
    assert "httponly" in completed.headers["set-cookie"].lower()

    session = client.get("/api/session")
    assert session.status_code == 200
    assert session.json()["authenticated"] is True
    assert session.json()["display_name"] == "咨询甲"
    with desk_context.session_factory() as db:
        stored = db.query(WebSession).one()
        assert stored.expires_at.replace(tzinfo=UTC) > datetime.now(UTC)


def test_oauth_rejects_state_mismatch_and_keeps_proxy_mount_prefix(
    desk_context: DeskContext,
) -> None:
    client = _oauth_client(desk_context, web_base_url="https://issues.example.test/kefu")

    started = client.get("/auth/login?next=%2Fkefu%2Fissues%2FKF-1", follow_redirects=False)
    login_query = parse_qs(urlparse(started.headers["location"]).query)
    assert login_query["callback"] == ["https://issues.example.test/kefu/auth/callback"]
    state = login_query["state"][0]

    invalid = client.get(
        "/auth/callback?code=one-time-code&state=incorrect-state",
        follow_redirects=False,
    )
    assert invalid.status_code == 401

    completed = client.get(
        f"/auth/callback?code=one-time-code&state={state}",
        follow_redirects=False,
    )
    assert completed.status_code == 302
    assert completed.headers["location"] == "/kefu/issues/KF-1"
