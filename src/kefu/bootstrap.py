"""Idempotently load the manually maintained MVP routing directory."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from kefu.config import Settings
from kefu.persistence.database import create_engine_from_url, create_session_factory
from kefu.persistence.models import (
    MembershipRole,
    Team,
    TeamKind,
    TeamMembership,
    User,
    WeComChannel,
)


class BootstrapConfigError(ValueError):
    pass


def load_routing_directory(
    session_factory: Callable[[], Session], data: Mapping[str, object]
) -> None:
    """Upsert stable users/teams/memberships/channels from a reviewed JSON file.

    It is intentionally only an initialization/configuration tool.  It never
    alters existing case ownership or history, and it fails when a channel is
    already bound to another internal team.
    """
    users_data = _list(data, "users")
    teams_data = _list(data, "teams")
    memberships_data = _list(data, "memberships")
    channels_data = _optional_list(data, "channels")
    global_admin_data = data.get("global_admin")
    now = datetime.now(UTC)
    with session_factory() as session, session.begin():
        users = _upsert_users(session, users_data)
        teams = _upsert_teams(session, teams_data)
        _upsert_memberships(session, memberships_data, users, teams, now)
        _upsert_channels(session, channels_data, teams, now)
        if global_admin_data is not None:
            _upsert_global_admin(session, global_admin_data)


def _upsert_users(session: Session, values: Sequence[object]) -> dict[str, User]:
    result: dict[str, User] = {}
    for raw in values:
        item = _object(raw, "users item")
        wecom_userid = _string(item, "wecom_userid")
        display_name = _string(item, "display_name")
        user = session.scalar(select(User).where(User.wecom_userid == wecom_userid))
        if user is None:
            user = User(
                id=uuid4(),
                wecom_userid=wecom_userid,
                display_name=display_name,
                is_global_admin=bool(item.get("is_global_admin", False)),
            )
            session.add(user)
        else:
            user.display_name = display_name
            user.active = bool(item.get("active", True))
            if "is_global_admin" in item:
                user.is_global_admin = bool(item["is_global_admin"])
        result[wecom_userid] = user
    return result


def _upsert_global_admin(session: Session, raw: object) -> User:
    item = _object(raw, "global_admin")
    wecom_userid = _string(item, "wecom_userid")
    raw_display_name = item.get("display_name", "事件中心总管理员")
    if not isinstance(raw_display_name, str) or not raw_display_name.strip():
        raise BootstrapConfigError("global_admin 的 display_name 不能为空")
    display_name = raw_display_name.strip()
    user = session.scalar(select(User).where(User.wecom_userid == wecom_userid))
    if user is None:
        user = User(
            id=uuid4(),
            wecom_userid=wecom_userid,
            display_name=display_name,
            active=True,
            is_global_admin=True,
        )
        session.add(user)
    else:
        user.display_name = display_name
        user.active = True
        user.is_global_admin = True
    return user


def _upsert_teams(session: Session, values: Sequence[object]) -> dict[str, Team]:
    result: dict[str, Team] = {}
    for raw in values:
        item = _object(raw, "teams item")
        key = _string(item, "key")
        if key in result:
            raise BootstrapConfigError(f"重复团队 key：{key}")
        name = _string(item, "name")
        try:
            kind = TeamKind(_string(item, "kind"))
        except ValueError as error:
            raise BootstrapConfigError(f"团队 {key} 的 kind 无效") from error
        lead_display_name = (
            _string(item, "lead_display_name") if kind is TeamKind.DEV else None
        )
        if lead_display_name is not None and len(lead_display_name) > 256:
            raise BootstrapConfigError(f"团队 {key} 的负责人展示名不能超过 256 个字符")
        team = session.scalar(select(Team).where(Team.kind == kind, Team.name == name))
        if team is None:
            team = Team(
                id=uuid4(),
                kind=kind,
                name=name,
                lead_display_name=lead_display_name,
                active=bool(item.get("active", True)),
            )
            session.add(team)
        else:
            team.active = bool(item.get("active", True))
            if kind is TeamKind.DEV:
                team.lead_display_name = lead_display_name
        result[key] = team
    return result


def _upsert_memberships(
    session: Session,
    values: Sequence[object],
    users: Mapping[str, User],
    teams: Mapping[str, Team],
    now: datetime,
) -> None:
    for raw in values:
        item = _object(raw, "memberships item")
        team = _reference(teams, _string(item, "team"), "团队")
        user = _reference(users, _string(item, "wecom_userid"), "用户")
        try:
            role = MembershipRole(str(item.get("role", MembershipRole.MEMBER.value)))
        except ValueError as error:
            raise BootstrapConfigError("成员角色只能是 member 或 admin") from error
        membership = session.get(TeamMembership, {"team_id": team.id, "user_id": user.id})
        if membership is None:
            session.add(
                TeamMembership(
                    team_id=team.id,
                    user_id=user.id,
                    role=role,
                    valid_from=now,
                    valid_until=None,
                )
            )
        else:
            membership.role = role
            membership.valid_until = None


def _upsert_channels(
    session: Session,
    values: Sequence[object],
    teams: Mapping[str, Team],
    now: datetime,
) -> None:
    for raw in values:
        item = _object(raw, "channels item")
        team = _reference(teams, _string(item, "team"), "团队")
        if team.kind is not TeamKind.DEV:
            raise BootstrapConfigError("只有研发责任团队可以绑定企业微信研发群")
        chatid = _string(item, "chatid")
        existing_for_chat = session.scalar(
            select(WeComChannel).where(WeComChannel.chatid == chatid)
        )
        if existing_for_chat is not None and existing_for_chat.team_id != team.id:
            raise BootstrapConfigError(f"群聊 {chatid} 已绑定到另一个研发责任团队")
        active_for_team = session.scalars(
            select(WeComChannel).where(
                WeComChannel.team_id == team.id,
                WeComChannel.active.is_(True),
            )
        ).all()
        for channel in active_for_team:
            channel.active = False
        channel = existing_for_chat
        if channel is None:
            channel = WeComChannel(id=uuid4(), team_id=team.id, chatid=chatid)
            session.add(channel)
        channel.active = True
        channel.initialized_at = now


def _list(data: Mapping[str, object], key: str) -> Sequence[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise BootstrapConfigError(f"配置必须包含数组字段：{key}")
    return value


def _optional_list(data: Mapping[str, object], key: str) -> Sequence[object]:
    if key not in data:
        return ()
    return _list(data, key)


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise BootstrapConfigError(f"{label} 必须是对象")
    return value


def _string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BootstrapConfigError(f"缺少非空字段：{key}")
    return value.strip()


def _reference[T](items: Mapping[str, T], key: str, label: str) -> T:
    try:
        return items[key]
    except KeyError as error:
        raise BootstrapConfigError(f"{label}引用不存在：{key}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="加载 MVP 人工维护的团队、成员和研发群路由")
    parser.add_argument("config", type=Path, help="已审核的 routing.json 路径")
    args = parser.parse_args()
    try:
        data = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise BootstrapConfigError("配置根节点必须是对象")
        settings = Settings.from_env()
        session_factory = create_session_factory(create_engine_from_url(settings.database_url))
        load_routing_directory(session_factory, data)
    except (OSError, json.JSONDecodeError, BootstrapConfigError) as error:
        parser.error(str(error))
