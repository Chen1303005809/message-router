from __future__ import annotations

from sqlalchemy import select

from kefu.bootstrap import load_routing_directory
from kefu.persistence.models import Team, TeamKind, TeamMembership, User, WeComChannel
from tests.conftest import DeskContext


def test_bootstrap_upserts_manual_routing_without_duplicate_memberships(
    desk_context: DeskContext,
) -> None:
    data = {
        "users": [
            {"wecom_userid": "new-consult", "display_name": "新咨询"},
            {"wecom_userid": "new-dev", "display_name": "新研发"},
        ],
        "teams": [
            {"key": "new-queue", "kind": "consult_queue", "name": "新咨询队列"},
            {
                "key": "new-dev-team",
                "kind": "dev",
                "name": "新研发团队",
                "lead_display_name": "研发团队负责人",
            },
        ],
        "memberships": [
            {"team": "new-queue", "wecom_userid": "new-consult", "role": "admin"},
            {"team": "new-dev-team", "wecom_userid": "new-dev", "role": "member"},
        ],
        "channels": [{"team": "new-dev-team", "chatid": "new-dev-chat"}],
    }
    load_routing_directory(desk_context.session_factory, data)
    load_routing_directory(desk_context.session_factory, data)
    without_channels = dict(data)
    del without_channels["channels"]
    load_routing_directory(desk_context.session_factory, without_channels)

    with desk_context.session_factory() as session:
        developer = session.scalar(select(User).where(User.wecom_userid == "new-dev"))
        team = session.scalar(
            select(Team).where(Team.kind == TeamKind.DEV, Team.name == "新研发团队")
        )
        assert developer is not None
        assert team is not None
        assert team.lead_display_name == "研发团队负责人"
        assert (
            session.get(TeamMembership, {"team_id": team.id, "user_id": developer.id}) is not None
        )
        assert session.scalar(select(WeComChannel).where(WeComChannel.chatid == "new-dev-chat"))


def test_bootstrap_can_initialize_global_admin_identity(desk_context: DeskContext) -> None:
    data = {
        "global_admin": {
            "wecom_userid": "center-admin",
            "display_name": "事件中心总管理员",
        },
        "users": [],
        "teams": [],
        "memberships": [],
    }
    load_routing_directory(desk_context.session_factory, data)
    load_routing_directory(desk_context.session_factory, data)

    with desk_context.session_factory() as session:
        admin = session.scalar(select(User).where(User.wecom_userid == "center-admin"))
        assert admin is not None
        assert admin.is_global_admin is True
        assert admin.display_name == "事件中心总管理员"
