"""Business operations behind the H5 organization and authorization center.

The event desk deliberately owns event transitions, while this module owns
the smaller but still security-sensitive organization changes: identities,
teams, memberships, global administrators, and team chat bindings.
The web layer only translates forms/JSON into these operations.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import Actor
from kefu.case_desk.errors import Conflict, Forbidden, NotFound, ValidationError
from kefu.persistence.models import (
    MembershipRole,
    Team,
    TeamKind,
    TeamMembership,
    User,
    WeComChannel,
)
from kefu.routing.directory import DatabaseRoutingDirectory, TeamChannel


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ManagedMembership:
    team_id: UUID
    team_name: str
    team_kind: TeamKind
    role: MembershipRole
    active: bool


@dataclass(frozen=True, slots=True)
class ManagedUser:
    id: UUID
    wecom_userid: str
    display_name: str
    active: bool
    is_global_admin: bool
    memberships: tuple[ManagedMembership, ...]


@dataclass(frozen=True, slots=True)
class ManagedTeamMember:
    user_id: UUID
    display_name: str
    wecom_userid: str
    role: MembershipRole
    active: bool


@dataclass(frozen=True, slots=True)
class ManagedTeam:
    id: UUID
    name: str
    kind: TeamKind
    active: bool
    channel_chatid: str | None
    channel_initialized: bool
    members: tuple[ManagedTeamMember, ...]


@dataclass(frozen=True, slots=True)
class AdminOverview:
    user_count: int
    active_user_count: int
    team_count: int
    consult_queue_count: int
    dev_team_count: int
    initialized_dev_team_count: int
    global_admin_count: int


class Administration:
    """Authorized organization management operations for a global admin."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        directory: DatabaseRoutingDirectory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._directory = directory or DatabaseRoutingDirectory()

    def is_global_admin(self, actor: Actor) -> bool:
        with self._session_factory() as session:
            try:
                return self._directory.is_global_admin(session, actor.user_id)
            except (NotFound, Forbidden):
                return False

    def has_global_admin(self) -> bool:
        """Return whether the instance has completed first-run admin setup."""
        with self._session_factory() as session:
            return (
                session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.is_global_admin.is_(True))
                )
                or 0
            ) > 0

    def overview(self, actor: Actor) -> AdminOverview:
        with self._session_factory() as session:
            self._assert_admin(session, actor)
            teams = session.scalars(select(Team)).all()
            dev_teams = [team for team in teams if team.kind is TeamKind.DEV and team.active]
            initialized_team_ids = {
                row.team_id
                for row in session.scalars(
                    select(WeComChannel).where(
                        WeComChannel.active.is_(True),
                        WeComChannel.initialized_at.is_not(None),
                    )
                )
            }
            return AdminOverview(
                user_count=self._count(session, User),
                active_user_count=self._count(session, User, User.active.is_(True)),
                team_count=sum(1 for team in teams if team.active),
                consult_queue_count=sum(
                    1
                    for team in teams
                    if team.active and team.kind is TeamKind.CONSULT_QUEUE
                ),
                dev_team_count=len(dev_teams),
                initialized_dev_team_count=sum(
                    1 for team in dev_teams if team.id in initialized_team_ids
                ),
                global_admin_count=self._count(
                    session, User, User.is_global_admin.is_(True), User.active.is_(True)
                ),
            )

    def list_users(self, actor: Actor, *, query: str = "") -> tuple[ManagedUser, ...]:
        with self._session_factory() as session:
            self._assert_admin(session, actor)
            statement = select(User).order_by(User.display_name.asc(), User.wecom_userid.asc())
            normalized = query.strip()
            if normalized:
                pattern = f"%{normalized}%"
                statement = statement.where(
                    User.display_name.ilike(pattern) | User.wecom_userid.ilike(pattern)
                )
            users = session.scalars(statement).all()
            return tuple(self._user_view(session, user) for user in users)

    def list_teams(self, actor: Actor) -> tuple[ManagedTeam, ...]:
        with self._session_factory() as session:
            self._assert_admin(session, actor)
            teams = session.scalars(select(Team).order_by(Team.kind.asc(), Team.name.asc())).all()
            return tuple(self._team_view(session, team) for team in teams)

    def create_user(
        self,
        actor: Actor,
        *,
        wecom_userid: str,
        display_name: str,
        is_global_admin: bool = False,
    ) -> ManagedUser:
        if not isinstance(is_global_admin, bool):
            raise ValidationError("总管理员标识必须是布尔值")
        normalized_userid, normalized_name = _identity_fields(wecom_userid, display_name)
        with self._session_factory() as session, session.begin():
            self._assert_admin(session, actor)
            existing = session.scalar(
                select(User).where(User.wecom_userid == normalized_userid)
            )
            if existing is not None:
                raise Conflict("该企业微信成员已经存在，请直接编辑已有成员")
            user = User(
                id=uuid4(),
                wecom_userid=normalized_userid,
                display_name=normalized_name,
                active=True,
                is_global_admin=is_global_admin,
            )
            session.add(user)
            session.flush()
            return self._user_view(session, user)

    def update_user(
        self,
        actor: Actor,
        user_id: UUID,
        *,
        display_name: str | None = None,
        active: bool | None = None,
        is_global_admin: bool | None = None,
    ) -> ManagedUser:
        if display_name is not None and (
            not isinstance(display_name, str) or not display_name.strip()
        ):
            raise ValidationError("成员姓名不能为空")
        if active is not None and not isinstance(active, bool):
            raise ValidationError("成员在岗标识必须是布尔值")
        if is_global_admin is not None and not isinstance(is_global_admin, bool):
            raise ValidationError("总管理员标识必须是布尔值")
        with self._session_factory() as session, session.begin():
            self._assert_admin(session, actor)
            user = session.get(User, user_id)
            if user is None:
                raise NotFound("用户不存在")
            removing_admin = user.is_global_admin and is_global_admin is False
            deactivating_admin = user.is_global_admin and active is False
            if removing_admin or deactivating_admin:
                self._assert_not_last_global_admin(session, user)
            if display_name is not None:
                user.display_name = display_name.strip()
            if active is not None:
                user.active = active
            if is_global_admin is not None:
                user.is_global_admin = is_global_admin
            session.flush()
            return self._user_view(session, user)

    def create_team(self, actor: Actor, *, kind: TeamKind, name: str) -> ManagedTeam:
        try:
            kind = TeamKind(kind)
        except (TypeError, ValueError) as error:
            raise ValidationError("团队类型只能是咨询队列或研发责任团队") from error
        normalized_name = _team_name(name)
        with self._session_factory() as session, session.begin():
            self._assert_admin(session, actor)
            existing = session.scalar(
                select(Team).where(Team.kind == kind, Team.name == normalized_name)
            )
            if existing is not None:
                raise Conflict("同类型团队名称已经存在")
            team = Team(id=uuid4(), kind=kind, name=normalized_name, active=True)
            session.add(team)
            session.flush()
            return self._team_view(session, team)

    def update_team(
        self,
        actor: Actor,
        team_id: UUID,
        *,
        name: str | None = None,
        active: bool | None = None,
    ) -> ManagedTeam:
        if name is not None:
            name = _team_name(name)
        if active is not None and not isinstance(active, bool):
            raise ValidationError("团队启用标识必须是布尔值")
        with self._session_factory() as session, session.begin():
            self._assert_admin(session, actor)
            team = session.get(Team, team_id)
            if team is None:
                raise NotFound("团队不存在")
            if name is not None and name != team.name:
                duplicate = session.scalar(
                    select(Team).where(
                        Team.kind == team.kind,
                        Team.name == name,
                        Team.id != team.id,
                    )
                )
                if duplicate is not None:
                    raise Conflict("同类型团队名称已经存在")
                team.name = name
            if active is not None:
                team.active = active
            session.flush()
            return self._team_view(session, team)

    def set_membership(
        self,
        actor: Actor,
        *,
        team_id: UUID,
        user_id: UUID,
        role: MembershipRole,
    ) -> ManagedTeam:
        try:
            role = MembershipRole(role)
        except (TypeError, ValueError) as error:
            raise ValidationError("成员角色只能是成员或团队管理员") from error
        with self._session_factory() as session, session.begin():
            self._assert_admin(session, actor)
            team = session.get(Team, team_id)
            if team is None:
                raise NotFound("团队不存在")
            if not team.active:
                raise ValidationError("停用团队不能添加成员")
            user = session.get(User, user_id)
            if user is None:
                raise NotFound("用户不存在")
            if not user.active:
                raise ValidationError("停用用户不能加入团队")
            now = utc_now()
            membership = session.get(
                TeamMembership,
                {"team_id": team.id, "user_id": user.id},
            )
            if membership is None:
                membership = TeamMembership(
                    team_id=team.id,
                    user_id=user.id,
                    role=role,
                    valid_from=now,
                    valid_until=None,
                )
                session.add(membership)
            else:
                membership.role = role
                membership.valid_from = now
                membership.valid_until = None
            session.flush()
            return self._team_view(session, team)

    def remove_membership(self, actor: Actor, *, team_id: UUID, user_id: UUID) -> ManagedTeam:
        with self._session_factory() as session, session.begin():
            self._assert_admin(session, actor)
            membership = session.get(
                TeamMembership,
                {"team_id": team_id, "user_id": user_id},
            )
            if membership is None:
                raise NotFound("该成员不在此团队中")
            membership.valid_until = utc_now()
            team = session.get(Team, team_id)
            if team is None:
                raise NotFound("团队不存在")
            session.flush()
            return self._team_view(session, team)

    def bind_channel(self, actor: Actor, *, team_id: UUID, chatid: str) -> TeamChannel:
        if not isinstance(chatid, str):
            raise ValidationError("群聊标识必须是文本")
        normalized_chatid = chatid.strip()
        if not normalized_chatid:
            raise ValidationError("群聊标识不能为空")
        with self._session_factory() as session, session.begin():
            self._assert_admin(session, actor)
            team = session.get(Team, team_id)
            if team is None:
                raise NotFound("团队不存在")
            return self._directory.bind_team_chat(
                session,
                actor_id=actor.user_id,
                team_name=team.name,
                chatid=normalized_chatid,
                team_kind=team.kind,
            )

    def _assert_admin(self, session: Session, actor: Actor) -> User:
        return self._directory.assert_global_admin(session, actor.user_id)

    def _assert_not_last_global_admin(self, session: Session, user: User) -> None:
        count = self._count(session, User, User.is_global_admin.is_(True), User.active.is_(True))
        if count <= 1:
            raise Conflict("系统至少需要保留一名在岗总管理员")

    @staticmethod
    def _count(session: Session, model: type[User] | type[Team], *conditions: object) -> int:
        statement = select(func.count()).select_from(model)
        if conditions:
            statement = statement.where(*conditions)
        return int(session.scalar(statement) or 0)

    def _user_view(self, session: Session, user: User) -> ManagedUser:
        now = utc_now()
        rows = session.execute(
            select(TeamMembership, Team)
            .join(Team, Team.id == TeamMembership.team_id)
            .where(TeamMembership.user_id == user.id)
            .order_by(Team.kind.asc(), Team.name.asc())
        ).all()
        memberships = tuple(
            ManagedMembership(
                team_id=team.id,
                team_name=team.name,
                team_kind=team.kind,
                role=membership.role,
                active=(
                    user.active
                    and team.active
                    and _as_utc(membership.valid_from) <= now
                    and (
                        membership.valid_until is None
                        or _as_utc(membership.valid_until) > now
                    )
                ),
            )
            for membership, team in rows
        )
        return ManagedUser(
            id=user.id,
            wecom_userid=user.wecom_userid,
            display_name=user.display_name,
            active=user.active,
            is_global_admin=user.is_global_admin,
            memberships=memberships,
        )

    def _team_view(self, session: Session, team: Team) -> ManagedTeam:
        now = utc_now()
        channel = session.scalar(
            select(WeComChannel)
            .where(WeComChannel.team_id == team.id, WeComChannel.active.is_(True))
            .order_by(WeComChannel.updated_at.desc())
        )
        rows = session.execute(
            select(TeamMembership, User)
            .join(User, User.id == TeamMembership.user_id)
            .where(TeamMembership.team_id == team.id)
            .order_by(User.display_name.asc(), User.wecom_userid.asc())
        ).all()
        members = tuple(
            ManagedTeamMember(
                user_id=user.id,
                display_name=user.display_name,
                wecom_userid=user.wecom_userid,
                role=membership.role,
                active=(
                    user.active
                    and team.active
                    and _as_utc(membership.valid_from) <= now
                    and (
                        membership.valid_until is None
                        or _as_utc(membership.valid_until) > now
                    )
                ),
            )
            for membership, user in rows
        )
        return ManagedTeam(
            id=team.id,
            name=team.name,
            kind=team.kind,
            active=team.active,
            channel_chatid=channel.chatid if channel is not None else None,
            channel_initialized=channel is not None and channel.initialized_at is not None,
            members=members,
        )


def initialize_global_admin(
    session_factory: Callable[[], Session],
    *,
    wecom_userid: str,
    display_name: str = "事件中心总管理员",
) -> ManagedUser:
    """Create or promote the one-time first global administrator identity.

    The identity is the enterprise-WeCom userid, so no separate password is
    introduced.  Re-running deployment bootstrap is safe and preserves all
    existing team memberships and event history.
    """
    normalized_userid, normalized_name = _identity_fields(wecom_userid, display_name)
    with session_factory() as session, session.begin():
        user = session.scalar(select(User).where(User.wecom_userid == normalized_userid))
        if user is None:
            user = User(
                id=uuid4(),
                wecom_userid=normalized_userid,
                display_name=normalized_name,
                active=True,
                is_global_admin=True,
            )
            session.add(user)
        else:
            user.display_name = normalized_name
            user.active = True
            user.is_global_admin = True
        session.flush()
        # A direct view keeps the function useful to both CLI/bootstrap and
        # the first-run H5 page without requiring an authenticated actor.
        return Administration(session_factory)._user_view(session, user)


def _identity_fields(wecom_userid: str, display_name: str) -> tuple[str, str]:
    if not isinstance(wecom_userid, str) or not isinstance(display_name, str):
        raise ValidationError("企业微信 userid 和成员姓名必须是文本")
    normalized_userid = wecom_userid.strip()
    normalized_name = display_name.strip()
    if not normalized_userid:
        raise ValidationError("企业微信 userid 不能为空")
    if len(normalized_userid) > 128:
        raise ValidationError("企业微信 userid 不能超过 128 个字符")
    if not normalized_name:
        raise ValidationError("成员姓名不能为空")
    if len(normalized_name) > 256:
        raise ValidationError("成员姓名不能超过 256 个字符")
    return normalized_userid, normalized_name


def _team_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValidationError("团队名称必须是文本")
    normalized_name = name.strip()
    if not normalized_name:
        raise ValidationError("团队名称不能为空")
    if len(normalized_name) > 256:
        raise ValidationError("团队名称不能超过 256 个字符")
    return normalized_name
