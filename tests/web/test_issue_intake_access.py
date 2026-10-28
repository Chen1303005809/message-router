from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import update

from kefu.config import Settings
from kefu.media.storage import InMemoryObjectStorage
from kefu.persistence.models import User, WeComChannel
from kefu.web.app import create_app
from tests.conftest import DeskContext


def _settings(
    *,
    auth_mode: str = "development",
    web_base_url: str = "https://events.example.test",
) -> Settings:
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
        web_base_url=web_base_url,
        wecom_corp_id="corp-id" if oauth_enabled else None,
        wecom_agent_id="1000001" if oauth_enabled else None,
        wecom_corp_secret="corp-secret" if oauth_enabled else None,
        dev_wecom_userid=None,
        session_cookie_secure=False,
    )


def _client(
    desk_context: DeskContext,
    *,
    auth_mode: str = "development",
    web_base_url: str = "https://events.example.test",
) -> TestClient:
    return TestClient(
        create_app(
            session_factory=desk_context.session_factory,
            settings=_settings(auth_mode=auth_mode, web_base_url=web_base_url),
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


def test_browser_forbidden_response_alerts_and_returns_to_same_origin_page(
    desk_context: DeskContext,
) -> None:
    client = _client(desk_context, web_base_url="https://events.example.test/kefu")

    response = client.get(
        "/admin",
        headers={
            "Accept": "text/html",
            "Referer": "http://testserver/kefu/issues?state=closed",
            "X-WeCom-UserId": "outsider",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "只有问题中心总管理员可以管理组织和授权" in response.text
    assert "window.alert(" in response.text
    assert 'window.location.replace("/kefu/issues?state=closed' in response.text
    assert "_kefu_forbidden_return=1" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_browser_forbidden_response_uses_issue_list_for_unsafe_or_missing_referer(
    desk_context: DeskContext,
) -> None:
    client = _client(desk_context)
    headers = {"Accept": "text/html", "X-WeCom-UserId": "outsider"}

    cross_origin = client.get(
        "/admin",
        headers={**headers, "Referer": "https://attacker.example/elsewhere"},
        follow_redirects=False,
    )
    missing_referer = client.get("/admin", headers=headers, follow_redirects=False)
    self_referer = client.get(
        "/admin",
        headers={**headers, "Referer": "http://testserver/admin"},
        follow_redirects=False,
    )

    for response in (cross_origin, missing_referer, self_referer):
        assert response.status_code == 403
        assert 'window.location.replace("/issues?_kefu_forbidden_return=1")' in response.text


def test_forbidden_recovery_marker_stops_redirect_loops(desk_context: DeskContext) -> None:
    client = _client(desk_context)

    response = client.get(
        "/admin?_kefu_forbidden_return=1",
        headers={"Accept": "text/html", "X-WeCom-UserId": "outsider"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "window.alert(" in response.text
    assert "window.location.replace(" not in response.text
    assert "window.history.back()" in response.text
    assert 'href="/issues"' in response.text


def test_api_forbidden_response_remains_json_and_h5_installs_fetch_alert(
    desk_context: DeskContext,
) -> None:
    client = _client(desk_context)
    headers = {"Accept": "text/html", "X-WeCom-UserId": "outsider"}

    api_response = client.get("/api/admin/overview", headers=headers)
    page_response = client.get("/issues", headers=headers)

    assert api_response.status_code == 403
    assert api_response.headers["content-type"].startswith("application/json")
    assert api_response.json()["detail"] == "只有问题中心总管理员可以管理组织和授权"
    assert page_response.status_code == 200
    assert "__kefuForbiddenFetchInstalled" in page_response.text
    assert "response.status === 403" in page_response.text
    assert "response.clone().json()" in page_response.text


def test_browser_forbidden_form_submission_returns_to_intake_page(
    desk_context: DeskContext,
) -> None:
    client = _client(desk_context)

    response = client.post(
        "/issues/draft",
        headers={
            "Accept": "text/html",
            "Referer": "http://testserver/issues/new",
            "X-WeCom-UserId": "outsider",
        },
        data={
            "pasted_text": "无权限创建的问题",
            "consult_queue_id": str(desk_context.teams["consult"]),
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "window.alert(" in response.text
    assert 'window.location.replace("/issues/new?_kefu_forbidden_return=1")' in response.text


def test_non_403_html_errors_keep_default_json_responses(desk_context: DeskContext) -> None:
    app = create_app(
        session_factory=desk_context.session_factory,
        settings=_settings(),
        storage=InMemoryObjectStorage(),
    )

    @app.get("/_test/http-error/{status_code}", include_in_schema=False)
    async def raise_http_error(status_code: int) -> None:
        raise HTTPException(status_code=status_code, detail="test error")

    @app.get("/_test/forbidden-detail", include_in_schema=False)
    async def raise_forbidden_with_markup() -> None:
        raise HTTPException(
            status_code=403,
            detail="</script><script>alert('unsafe')</script>",
        )

    client = TestClient(app)
    for status_code in (401, 404, 409, 422):
        response = client.get(
            f"/_test/http-error/{status_code}",
            headers={"Accept": "text/html"},
        )
        assert response.status_code == status_code
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["detail"] == "test error"

    forbidden_response = client.get(
        "/_test/forbidden-detail",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert forbidden_response.status_code == 403
    assert "</script><script>alert('unsafe')</script>" not in forbidden_response.text
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in forbidden_response.text
