from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from kefu.case_desk.contracts import Actor, CreateCase, TextPart
from kefu.config import Settings
from kefu.media.storage import InMemoryObjectStorage, MediaIngestor
from kefu.persistence.models import (
    CaseEntryKind,
    CaseStatus,
    DeliveryItem,
    MessageDraft,
    MessageDraftPart,
    PartKind,
)
from kefu.web.app import create_app
from kefu.web.auth import WebAuthenticator
from kefu.wecom.transport import InboundImagePart
from tests.conftest import DeskContext


def settings(
    *, auth_mode: str = "development", web_base_url: str = "https://events.example.test"
) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite://",
        object_storage_backend="local",
        object_storage_root=Path("/private/tmp/kefu-web-media"),
        s3_endpoint_url=None,
        s3_bucket="kefu-media",
        s3_access_key_id=None,
        s3_secret_access_key=None,
        wecom_transport="fake",
        auth_mode=auth_mode,
        web_base_url=web_base_url,
        wecom_corp_id="corp-id" if auth_mode == "wecom_oauth" else None,
        wecom_agent_id="1000001" if auth_mode == "wecom_oauth" else None,
        wecom_corp_secret="corp-secret" if auth_mode == "wecom_oauth" else None,
        dev_wecom_userid=None,
        session_cookie_secure=False,
    )


def test_h5_lists_events_renders_detail_and_posts_formal_message(desk_context: DeskContext) -> None:
    created = desk_context.desk.execute(
        CreateCase(
            title="H5 验收",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            parts=(TextPart("初始内容"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    app = create_app(session_factory=desk_context.session_factory, settings=settings())
    client = TestClient(app)
    headers = {"X-WeCom-UserId": "consult-a"}

    listing = client.get("/events", headers=headers)
    assert listing.status_code == 200
    assert "H5 验收" in listing.text
    assert "事件数据一览" in listing.text
    assert 'href="/events/new"' in listing.text
    detail = client.get(f"/events/{created.case_ref}", headers=headers)
    assert detail.status_code == 200
    assert "初始内容" in detail.text
    api = client.get("/api/events", headers=headers)
    assert api.status_code == 200
    assert api.json()["items"][0]["case_ref"] == created.case_ref

    current = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["consult_a"])
    )
    posted = client.post(
        f"/events/{created.case_ref}/messages",
        headers=headers,
        data={"version": str(current.version), "text": "通过 H5 补充信息"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    updated = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["consult_a"])
    )
    assert updated.entries[-1].parts[0].text == "通过 H5 补充信息"


def test_event_detail_shows_consultant_handover_and_separated_deadline_controls(
    desk_context: DeskContext,
) -> None:
    created = desk_context.desk.execute(
        CreateCase(
            title="详情页转交验收",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            parts=(TextPart("转交和期限管理验收"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    app = create_app(session_factory=desk_context.session_factory, settings=settings())
    client = TestClient(app)
    detail = client.get(f"/events/{created.case_ref}", headers={"X-WeCom-UserId": "consult-a"})

    assert detail.status_code == 200
    assert 'aria-label="咨询经办人转交"' in detail.text
    assert 'name="new_consultant_id"' in detail.text
    assert 'name="new_developer_id"' not in detail.text
    assert str(desk_context.users["consult_b"]) in detail.text
    assert "研发团队负责人（仅显示）" in detail.text
    assert "一组负责人" in detail.text
    assert str(desk_context.users["dev_b"]) not in detail.text
    assert str(desk_context.users["dev_c"]) not in detail.text
    api = client.get(
        f"/api/events/{created.case_ref}", headers={"X-WeCom-UserId": "consult-a"}
    )
    assert api.status_code == 200
    assert api.json()["current_dev_team_lead_display_name"] == "一组负责人"
    assert "current_developer_name" not in api.json()

    assert "调整当前截止时间" in detail.text
    assert "设置临近期限提醒" in detail.text
    assert "应用截止时间调整" in detail.text
    assert "保存提醒设置" in detail.text
    assert "此操作只改变截止时间，不会改变临近期限提醒设置。" in detail.text
    assert "此操作只改变临近期限提醒设置，不会调整截止时间。" in detail.text

    current = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["consult_a"])
    )
    response = client.post(
        f"/events/{created.case_ref}/transfer-consultant",
        headers={"X-WeCom-UserId": "consult-a"},
        data={
            "version": str(current.version),
            "new_consultant_id": str(desk_context.users["consult_b"]),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    updated = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["consult_b"])
    )
    assert updated.current_consultant_id == desk_context.users["consult_b"]

    removed_developer_transfer = client.post(
        f"/events/{created.case_ref}/transfer-developer",
        headers={"X-WeCom-UserId": "consult-b"},
        data={
            "version": str(updated.version),
            "new_developer_id": str(desk_context.users["dev_c"]),
        },
        follow_redirects=False,
    )
    assert removed_developer_transfer.status_code == 404


def test_transfer_controls_are_hidden_from_ordinary_team_members(
    desk_context: DeskContext,
) -> None:
    created = desk_context.desk.execute(
        CreateCase(
            title="转交权限验收",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            parts=(TextPart("检查转交权限"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    app = create_app(session_factory=desk_context.session_factory, settings=settings())
    client = TestClient(app)

    ordinary_consultant = client.get(
        f"/events/{created.case_ref}", headers={"X-WeCom-UserId": "consult-b"}
    )
    assert ordinary_consultant.status_code == 200
    assert 'aria-label="咨询经办人转交"' not in ordinary_consultant.text

    current_developer = client.get(
        f"/events/{created.case_ref}", headers={"X-WeCom-UserId": "dev-a"}
    )
    assert current_developer.status_code == 200
    assert 'aria-label="咨询经办人转交"' not in current_developer.text
    developer_case = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["dev_a"])
    )
    developer_transfer = client.post(
        f"/events/{created.case_ref}/transfer-developer",
        headers={"X-WeCom-UserId": "dev-a"},
        data={
            "version": str(developer_case.version),
            "new_developer_id": str(desk_context.users["dev_b"]),
        },
        follow_redirects=False,
    )
    assert developer_transfer.status_code == 404

    developer_admin = client.get(
        f"/events/{created.case_ref}", headers={"X-WeCom-UserId": "dev-admin"}
    )
    assert developer_admin.status_code == 200
    assert 'aria-label="咨询经办人转交"' in developer_admin.text


def test_h5_status_buttons_follow_permissions_and_update_timeline(
    desk_context: DeskContext,
) -> None:
    created = desk_context.desk.execute(
        CreateCase(
            title="状态流转验收",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            parts=(TextPart("等待状态跟进"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    app = create_app(session_factory=desk_context.session_factory, settings=settings())
    client = TestClient(app)
    consult_headers = {"X-WeCom-UserId": "consult-a"}
    developer_headers = {"X-WeCom-UserId": "dev-a"}

    detail = client.get(f"/events/{created.case_ref}", headers=consult_headers)
    assert detail.status_code == 200
    assert detail.text.index('aria-label="事件状态操作"') < detail.text.index("事件时间线")
    assert 'name="status" value="in_progress"' not in detail.text
    assert 'name="status" value="waiting_customer"' not in detail.text
    assert f'action="/events/{created.case_ref}/close"' in detail.text

    developer_case = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["dev_a"])
    )
    developer_detail = client.get(f"/events/{created.case_ref}", headers=developer_headers)
    assert 'name="status" value="in_progress"' in developer_detail.text
    assert 'class="status-action-accept"' in developer_detail.text
    forbidden = client.post(
        f"/events/{created.case_ref}/status",
        headers=developer_headers,
        data={"version": str(developer_case.version), "status": "waiting_customer"},
    )
    assert forbidden.status_code == 403

    started = client.post(
        f"/events/{created.case_ref}/status",
        headers=developer_headers,
        data={"version": str(developer_case.version), "status": "in_progress"},
        follow_redirects=False,
    )
    assert started.status_code == 303
    in_progress = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["consult_a"])
    )
    assert in_progress.status is CaseStatus.IN_PROGRESS
    assert in_progress.entries[-1].kind is CaseEntryKind.STATUS_CHANGED
    processing_detail = client.get(f"/events/{created.case_ref}", headers=consult_headers)
    assert 'name="status" value="waiting_customer"' in processing_detail.text
    assert 'name="status" value="suspended"' in processing_detail.text
    assert 'class="status-action-feedback"' in processing_detail.text
    assert 'class="status-action-suspend"' in processing_detail.text
    assert 'class="status-action-close"' in processing_detail.text

    waiting = client.post(
        f"/events/{created.case_ref}/status",
        headers=consult_headers,
        data={"version": str(in_progress.version), "status": "waiting_customer"},
        follow_redirects=False,
    )
    assert waiting.status_code == 303
    waiting_case = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["consult_a"])
    )
    assert waiting_case.status is CaseStatus.WAITING_CUSTOMER
    detail_api = client.get(f"/api/events/{created.case_ref}", headers=consult_headers)
    assert detail_api.json()["status"] == CaseStatus.WAITING_CUSTOMER.value

    closed = client.post(
        f"/events/{created.case_ref}/close",
        headers=consult_headers,
        data={"version": str(waiting_case.version)},
        follow_redirects=False,
    )
    assert closed.status_code == 303
    closed_case = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["consult_a"])
    )
    assert closed_case.status is CaseStatus.CLOSED

    reopened = client.post(
        f"/events/{created.case_ref}/reopen",
        headers=consult_headers,
        data={"version": str(closed_case.version), "waiting_on": "dev"},
        follow_redirects=False,
    )
    assert reopened.status_code == 303
    reopened_case = desk_context.desk.get_case(
        created.case_ref or "", Actor(desk_context.users["consult_a"])
    )
    assert reopened_case.status is CaseStatus.IN_PROGRESS


def test_h5_creates_case_from_draft_and_serves_authorized_image(desk_context: DeskContext) -> None:
    storage = InMemoryObjectStorage()
    media_id = MediaIngestor(desk_context.session_factory, storage).ingest(
        InboundImagePart(data=b"web-image", mime_type="image/png")
    )
    draft_id = uuid4()

    with desk_context.session_factory() as session, session.begin():
        session.add(
            MessageDraft(
                id=draft_id,
                owner_user_id=desk_context.users["consult_a"],
                source_msgid="web-draft",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        session.add_all(
            [
                MessageDraftPart(
                    draft_id=draft_id,
                    position=0,
                    kind=PartKind.TEXT,
                    text="带图草稿",
                    media_id=None,
                ),
                MessageDraftPart(
                    draft_id=draft_id,
                    position=1,
                    kind=PartKind.IMAGE,
                    text=None,
                    media_id=media_id,
                ),
            ]
        )

    app = create_app(
        session_factory=desk_context.session_factory,
        settings=settings(),
        storage=storage,
    )
    client = TestClient(app)
    headers = {"X-WeCom-UserId": "consult-a"}
    form = client.get(f"/drafts/{draft_id}/new", headers=headers)
    assert form.status_code == 200
    assert "带图草稿" in form.text
    created = client.post(
        f"/drafts/{draft_id}/new",
        headers=headers,
        data={
            "title": "网页创建事件",
            "customer_name": "测试客户",
            "consult_queue_id": str(desk_context.teams["consult"]),
            "dev_team_id": str(desk_context.teams["dev_a"]),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    case_ref = created.headers["location"].rsplit("/", maxsplit=1)[-1]
    image = client.get(f"/events/{case_ref}/media/{media_id}", headers=headers)
    assert image.status_code == 200
    assert image.content == b"web-image"


def test_h5_homepage_creates_case_with_multiple_images(desk_context: DeskContext) -> None:
    storage = InMemoryObjectStorage()
    app = create_app(
        session_factory=desk_context.session_factory,
        settings=settings(),
        storage=storage,
    )
    client = TestClient(app)
    headers = {"X-WeCom-UserId": "consult-a"}

    homepage = client.get("/events", headers=headers)
    assert homepage.status_code == 200
    assert "全部进行中" in homepage.text
    assert "待咨询处理" in homepage.text
    assert 'href="/events/new"' in homepage.text

    form = client.get("/events/new", headers=headers)
    assert form.status_code == 200
    assert 'enctype="multipart/form-data"' in form.text
    assert 'name="images"' in form.text
    assert 'name="dev_team_id"' in form.text
    assert "研发一组（负责人：一组负责人）" in form.text
    assert 'name="developer_id"' not in form.text

    created = client.post(
        "/events/new",
        headers=headers,
        data={
            "title": "H5 多图事件",
            "description": "客户登录后页面空白",
            "customer_name": "网页客户",
            "consult_queue_id": str(desk_context.teams["consult"]),
            "dev_team_id": str(desk_context.teams["dev_a"]),
            "priority": "normal",
        },
        files=[
            ("images", ("screen-a.png", b"first-image", "image/png")),
            ("images", ("screen-b.jpg", b"second-image", "image/jpeg")),
        ],
        follow_redirects=False,
    )
    assert created.status_code == 303
    case_ref = created.headers["location"].rsplit("/", maxsplit=1)[-1]
    view = desk_context.desk.get_case(case_ref, Actor(desk_context.users["consult_a"]))
    assert view.title == "H5 多图事件"
    assert view.customer_name == "网页客户"
    assert [part.kind for part in view.entries[0].parts] == [
        PartKind.TEXT,
        PartKind.IMAGE,
        PartKind.IMAGE,
    ]
    first_image, second_image = view.entries[0].parts[1:]
    assert client.get(
        f"/events/{case_ref}/media/{first_image.media_id}", headers=headers
    ).content == b"first-image"
    assert client.get(
        f"/events/{case_ref}/media/{second_image.media_id}", headers=headers
    ).content == b"second-image"


class FakeOAuthClient:
    def authorize_url(self, callback_url: str, state: str) -> str:
        return f"https://oauth.example.test/?callback={callback_url}&state={state}"

    def user_id_for_code(self, code: str) -> str:
        assert code == "one-time-code"
        return "consult-a"


def test_oauth_callback_creates_server_side_session(desk_context: DeskContext) -> None:
    configured = settings(auth_mode="wecom_oauth")
    authenticator = WebAuthenticator(
        desk_context.session_factory,
        configured,
        oauth_client=FakeOAuthClient(),
    )
    app = create_app(
        session_factory=desk_context.session_factory,
        settings=configured,
        authenticator=authenticator,
    )
    client = TestClient(app)
    started = client.get("/events", follow_redirects=False)
    assert started.status_code == 302
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    completed = client.get(
        f"/auth/callback?code=one-time-code&state={state}", follow_redirects=False
    )
    assert completed.status_code == 302
    assert completed.headers["location"] == "/events"
    listed = client.get("/api/events")
    assert listed.status_code == 200


def test_oauth_login_return_preserves_event_history_card_destination(
    desk_context: DeskContext,
) -> None:
    created = desk_context.desk.execute(
        CreateCase(
            title="OAuth 详情跳转",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            parts=(TextPart("初始内容"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    configured = settings(auth_mode="wecom_oauth")
    authenticator = WebAuthenticator(
        desk_context.session_factory,
        configured,
        oauth_client=FakeOAuthClient(),
    )
    app = create_app(
        session_factory=desk_context.session_factory,
        settings=configured,
        authenticator=authenticator,
    )
    client = TestClient(app)
    destination = f"/events/{created.case_ref}"
    started = client.get(destination, follow_redirects=False)
    assert started.status_code == 302
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    completed = client.get(
        f"/auth/callback?code=one-time-code&state={state}", follow_redirects=False
    )
    assert completed.status_code == 302
    assert completed.headers["location"] == destination
    detail = client.get(destination)
    assert detail.status_code == 200
    assert "OAuth 详情跳转" in detail.text


def test_h5_preserves_a_reverse_proxy_mount_prefix(desk_context: DeskContext) -> None:
    created = desk_context.desk.execute(
        CreateCase(
            title="代理路径验收",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            parts=(TextPart("初始内容"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    prefixed = settings(web_base_url="https://events.example.test/kefu")
    app = create_app(session_factory=desk_context.session_factory, settings=prefixed)
    client = TestClient(app)
    headers = {"X-WeCom-UserId": "consult-a"}

    root = client.get("/", follow_redirects=False)
    assert root.headers["location"] == "/kefu/events"
    listing = client.get("/events", headers=headers)
    assert 'href="/kefu/events?state=open"' in listing.text
    assert f'href="/kefu/events/{created.case_ref}"' in listing.text
    assert 'href="/kefu/events/new"' in listing.text
    create_form = client.get("/events/new", headers=headers)
    assert create_form.status_code == 200
    assert 'action="/kefu/events/new"' in create_form.text
    created_from_h5 = client.post(
        "/events/new",
        headers=headers,
        data={
            "title": "挂载路径新事件",
            "description": "问题描述",
            "customer_name": "路径客户",
            "consult_queue_id": str(desk_context.teams["consult"]),
            "dev_team_id": str(desk_context.teams["dev_a"]),
        },
        follow_redirects=False,
    )
    assert created_from_h5.status_code == 303
    assert created_from_h5.headers["location"].startswith("/kefu/events/")
    new_ref = created_from_h5.headers["location"].rsplit("/", maxsplit=1)[-1]
    new_view = desk_context.desk.get_case(new_ref, Actor(desk_context.users["consult_a"]))
    with desk_context.session_factory() as session:
        delivery_items = session.scalars(
            select(DeliveryItem)
            .where(DeliveryItem.delivery_id == new_view.deliveries[0].id)
            .order_by(DeliveryItem.position)
        ).all()
    assert len(delivery_items) == 1
    assert "template_card" not in delivery_items[0].payload_json
    assert "content" in delivery_items[0].payload_json

    oauth_settings = settings(
        auth_mode="wecom_oauth", web_base_url="https://events.example.test/kefu"
    )
    oauth_authenticator = WebAuthenticator(
        desk_context.session_factory,
        oauth_settings,
        oauth_client=FakeOAuthClient(),
    )
    oauth_app = create_app(
        session_factory=desk_context.session_factory,
        settings=oauth_settings,
        authenticator=oauth_authenticator,
    )
    oauth_client = TestClient(oauth_app)
    started = oauth_client.get("/events", follow_redirects=False)
    assert "callback=https://events.example.test/kefu/auth/callback" in started.headers["location"]
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    completed = oauth_client.get(
        f"/auth/callback?code=one-time-code&state={state}", follow_redirects=False
    )
    assert completed.headers["location"] == "/kefu/events"
