from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from kefu.admin.service import Administration, initialize_global_admin
from kefu.case_desk.contracts import Actor, CreateCase, TextPart
from kefu.case_desk.errors import Conflict, Forbidden
from kefu.persistence.models import TeamKind
from kefu.web.app import create_app
from tests.conftest import DeskContext
from tests.web.test_app import settings


def test_visual_setup_creates_global_admin_and_manages_members_and_teams(
    desk_context: DeskContext,
) -> None:
    app = create_app(session_factory=desk_context.session_factory, settings=settings())
    client = TestClient(app)

    setup = client.post(
        "/setup",
        headers={"X-WeCom-UserId": "center-admin"},
        data={
            "wecom_userid": "center-admin",
            "display_name": "事件中心管理员",
        },
        follow_redirects=False,
    )
    assert setup.status_code == 303
    assert setup.headers["location"] == "/admin"

    headers = {"X-WeCom-UserId": "center-admin"}
    page = client.get("/admin", headers=headers)
    assert page.status_code == 200
    assert "组织与授权" in page.text
    assert "咨询队列" in page.text
    assert "研发一组" in page.text

    created_user = client.post(
        "/api/admin/users",
        headers=headers,
        json={"wecom_userid": "consult-c", "display_name": "咨询丙"},
    )
    assert created_user.status_code == 201
    user_id = UUID(created_user.json()["id"])

    created_team = client.post(
        "/api/admin/teams",
        headers=headers,
        json={"kind": "consult_queue", "name": "华东咨询队列"},
    )
    assert created_team.status_code == 201
    team_id = UUID(created_team.json()["id"])

    created_dev_team = client.post(
        "/api/admin/teams",
        headers=headers,
        json={
            "kind": "dev",
            "name": "支付研发组",
            "lead_display_name": "支付负责人",
        },
    )
    assert created_dev_team.status_code == 201
    assert created_dev_team.json()["lead_display_name"] == "支付负责人"
    assert created_dev_team.json()["members"] == []

    membership = client.post(
        f"/api/admin/teams/{team_id}/members",
        headers=headers,
        json={"user_id": str(user_id), "role": "admin"},
    )
    assert membership.status_code == 200
    member = next(
        item for item in membership.json()["members"] if item["user_id"] == str(user_id)
    )
    assert member["role"] == "admin"
    assert member["active"] is True

    users = client.get("/api/admin/users?q=咨询丙", headers=headers)
    assert users.status_code == 200
    assert users.json()["items"][0]["memberships"][0]["team_name"] == "华东咨询队列"

    removed = client.delete(
        f"/api/admin/teams/{team_id}/members/{user_id}",
        headers=headers,
    )
    assert removed.status_code == 200
    assert next(
        item for item in removed.json()["members"] if item["user_id"] == str(user_id)
    )["active"] is False


def test_global_admin_can_see_all_events_and_cannot_remove_last_admin(
    desk_context: DeskContext,
) -> None:
    bootstrapped = initialize_global_admin(
        desk_context.session_factory,
        wecom_userid="center-admin",
        display_name="事件中心管理员",
    )
    admin = Administration(desk_context.session_factory)
    actor = Actor(bootstrapped.id)

    # The bootstrap identity is allowed to perform a cross-team setup action
    # without being added to a business team first.
    created_team = admin.create_team(
        actor,
        kind=TeamKind.DEV,
        name="新研发组",
        lead_display_name="新研发负责人",
    )
    assert created_team.kind is TeamKind.DEV
    assert created_team.lead_display_name == "新研发负责人"

    case = desk_context.desk.execute(
        CreateCase(
            title="总管理员可见性",
            consult_queue_id=desk_context.teams["consult"],
            dev_team_id=desk_context.teams["dev_a"],
            parts=(TextPart("内容"),),
        ),
        Actor(desk_context.users["consult_a"]),
    )
    visible = desk_context.desk.get_case(case.case_ref or "", actor)
    assert visible.title == "总管理员可见性"

    with pytest.raises(Conflict):
        admin.update_user(actor, actor.user_id, is_global_admin=False)

    with pytest.raises(Forbidden):
        admin.create_team(
            Actor(desk_context.users["outsider"]),
            kind=TeamKind.CONSULT_QUEUE,
            name="不应创建",
        )


def test_configured_admin_is_initialized_on_web_startup(desk_context: DeskContext) -> None:
    configured = replace(
        settings(),
        initial_admin_wecom_userid="env-admin",
        initial_admin_display_name="部署管理员",
    )
    app = create_app(session_factory=desk_context.session_factory, settings=configured)
    client = TestClient(app)

    page = client.get("/settings/routing", headers={"X-WeCom-UserId": "env-admin"})
    assert page.status_code == 200
    assert "部署管理员" in page.text
    assert "组织与授权" in page.text
    assert client.get("/api/admin/overview", headers={"X-WeCom-UserId": "env-admin"}).json()[
        "global_admin_count"
    ] == 1
