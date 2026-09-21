"""Database-backed organization routing.

This is the only component that knows how active memberships and a current
WeCom group become a delivery target.  A future directory-sync adapter can
replace it without changing the case desk commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kefu.case_desk.errors import Forbidden, NotFound, RoutingUnavailable
from kefu.persistence.models import (
    Case,
    MembershipRole,
    Team,
    TeamKind,
    TeamMembership,
    User,
    WeComChannel,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class TeamChannel:
    team_id: UUID
    chatid: str


class DatabaseRoutingDirectory:
    """Resolve stable people/team IDs while failing closed on ambiguity."""

    def _current_membership_conditions(self, now: datetime) -> tuple[object, object]:
        return (
            TeamMembership.valid_from <= now,
            or_(TeamMembership.valid_until.is_(None), TeamMembership.valid_until > now),
        )

    def get_user(self, session: Session, user_id: UUID, *, require_active: bool = True) -> User:
        user = session.get(User, user_id)
        if user is None:
            raise NotFound("用户不存在")
        if require_active and not user.active:
            raise RoutingUnavailable("目标用户已停用")
        return user

    def get_user_by_wecom_userid(
        self, session: Session, wecom_userid: str, *, require_active: bool = True
    ) -> User:
        user = session.scalar(select(User).where(User.wecom_userid == wecom_userid))
        if user is None:
            raise NotFound("企业微信用户尚未初始化")
        if require_active and not user.active:
            raise RoutingUnavailable("企业微信用户已停用")
        return user

    def get_team(self, session: Session, team_id: UUID, *, kind: TeamKind | None = None) -> Team:
        team = session.get(Team, team_id)
        if team is None:
            raise NotFound("团队不存在")
        if not team.active:
            raise RoutingUnavailable("目标团队已停用")
        if kind is not None and team.kind is not kind:
            raise ValidationErrorForDirectory("团队类型不匹配")
        return team

    def active_membership(
        self, session: Session, *, user_id: UUID, team_id: UUID, now: datetime | None = None
    ) -> TeamMembership | None:
        now = now or utc_now()
        valid_from, valid_until = self._current_membership_conditions(now)
        return session.scalar(
            select(TeamMembership).where(
                TeamMembership.user_id == user_id,
                TeamMembership.team_id == team_id,
                valid_from,
                valid_until,
            )
        )

    def is_member(self, session: Session, *, user_id: UUID, team_id: UUID) -> bool:
        return self.active_membership(session, user_id=user_id, team_id=team_id) is not None

    def is_admin(self, session: Session, *, user_id: UUID, team_id: UUID) -> bool:
        membership = self.active_membership(session, user_id=user_id, team_id=team_id)
        return membership is not None and membership.role is MembershipRole.ADMIN

    def is_global_admin(self, session: Session, user_id: UUID) -> bool:
        """Return whether an active identity has cross-team administration rights."""
        user = self.get_user(session, user_id)
        return user.is_global_admin

    def assert_global_admin(self, session: Session, user_id: UUID) -> User:
        """Require the dedicated global-admin flag for organization changes."""
        user = self.get_user(session, user_id)
        if not user.is_global_admin:
            raise Forbidden("只有事件中心总管理员可以管理组织和授权")
        return user

    def route_developer(self, session: Session, developer_id: UUID) -> TeamChannel:
        """Resolve exactly one active development team and its one active chat."""
        self.get_user(session, developer_id)
        now = utc_now()
        valid_from, valid_until = self._current_membership_conditions(now)
        rows = session.scalars(
            select(Team)
            .join(TeamMembership, TeamMembership.team_id == Team.id)
            .where(
                TeamMembership.user_id == developer_id,
                Team.kind == TeamKind.DEV,
                Team.active.is_(True),
                valid_from,
                valid_until,
            )
        ).all()
        if len(rows) != 1:
            raise RoutingUnavailable("研发人员没有唯一的有效责任团队")
        return self.active_channel(session, rows[0].id)

    def active_channel(self, session: Session, team_id: UUID) -> TeamChannel:
        """Resolve the required active channel for a development team."""
        channel = self._active_channel(session, team_id, kind=TeamKind.DEV, optional=False)
        assert channel is not None
        return channel

    def active_consult_channel(self, session: Session, queue_id: UUID) -> TeamChannel | None:
        """Resolve an optional active channel for a consultation queue.

        Queues can continue using consultant DMs until a consultation group is
        bound. Inactive queues also do not receive group copies, preserving the
        primary DM route for existing cases.
        """
        team = session.get(Team, queue_id)
        if team is None:
            raise NotFound("咨询队列不存在")
        if team.kind is not TeamKind.CONSULT_QUEUE:
            raise ValidationErrorForDirectory("团队类型不匹配")
        if not team.active:
            return None
        return self._active_channel(session, queue_id, kind=TeamKind.CONSULT_QUEUE, optional=True)

    def _active_channel(
        self,
        session: Session,
        team_id: UUID,
        *,
        kind: TeamKind,
        optional: bool,
    ) -> TeamChannel | None:
        team = self.get_team(session, team_id, kind=kind)
        channels = session.scalars(
            select(WeComChannel).where(
                WeComChannel.team_id == team.id,
                WeComChannel.active.is_(True),
            )
        ).all()
        if optional and not channels:
            return None
        if len(channels) != 1 or channels[0].initialized_at is None:
            label = "研发团队" if kind is TeamKind.DEV else "咨询队列"
            raise RoutingUnavailable(f"{label}没有唯一且已初始化的群聊通道")
        return TeamChannel(team_id=team.id, chatid=channels[0].chatid)

    def bind_dev_chat(
        self, session: Session, *, actor_id: UUID, team_name: str, chatid: str
    ) -> TeamChannel:
        """Bind the current group to one development team."""
        return self.bind_team_chat(
            session,
            actor_id=actor_id,
            team_name=team_name,
            chatid=chatid,
            team_kind=TeamKind.DEV,
        )

    def bind_team_chat(
        self,
        session: Session,
        *,
        actor_id: UUID,
        team_name: str,
        chatid: str,
        team_kind: TeamKind,
    ) -> TeamChannel:
        """Bind the current group to a team, under a team-admin check.

        This is the one safe exception to the normal manually-maintained
        directory: a group callback is the only reliable way for the bot to
        discover its opaque ``chatid``. Existing cases retain their stable team
        ownership; only that team's live communication channel changes.
        """
        normalized_team_name = team_name.strip()
        normalized_chatid = chatid.strip()
        if not normalized_team_name or not normalized_chatid:
            raise ValidationErrorForDirectory("团队名称和群聊标识不能为空")
        teams = session.scalars(
            select(Team).where(
                Team.kind == team_kind,
                Team.name == normalized_team_name,
                Team.active.is_(True),
            )
        ).all()
        if len(teams) != 1:
            label = "在岗研发责任团队" if team_kind is TeamKind.DEV else "在岗咨询队列"
            raise RoutingUnavailable(f"未找到唯一的{label}")
        team = teams[0]
        self.get_user(session, actor_id)
        if not (
            self.is_global_admin(session, actor_id)
            or self.is_admin(session, user_id=actor_id, team_id=team.id)
        ):
            label = "研发团队管理员" if team_kind is TeamKind.DEV else "咨询队列管理员"
            chat_label = "研发群" if team_kind is TeamKind.DEV else "咨询群"
            raise Forbidden(f"只有该{label}可以绑定{chat_label}")
        existing = session.scalar(
            select(WeComChannel).where(WeComChannel.chatid == normalized_chatid)
        )
        if existing is not None and existing.team_id != team.id:
            raise RoutingUnavailable("该企业微信群已绑定到另一个团队")
        for active_channel in session.scalars(
            select(WeComChannel).where(
                WeComChannel.team_id == team.id,
                WeComChannel.active.is_(True),
            )
        ):
            active_channel.active = False
        channel = existing
        if channel is None:
            channel = WeComChannel(id=uuid4(), team_id=team.id, chatid=normalized_chatid)
            session.add(channel)
        channel.active = True
        channel.initialized_at = utc_now()
        return TeamChannel(team_id=team.id, chatid=normalized_chatid)

    def assert_case_visible(self, session: Session, viewer_id: UUID, case: Case) -> None:
        self.get_user(session, viewer_id)
        if self.is_global_admin(session, viewer_id):
            return
        if self.is_member(session, user_id=viewer_id, team_id=case.consult_queue_id):
            return
        if self.is_member(session, user_id=viewer_id, team_id=case.current_dev_team_id):
            return
        raise Forbidden("你没有查看此事件的权限")

    def side_for_actor(
        self, session: Session, viewer_id: UUID, case: Case, requested_side: object | None
    ) -> object:
        """Return EntrySide lazily to avoid a circular import in the directory."""
        from kefu.persistence.models import EntrySide

        self.get_user(session, viewer_id)
        if self.is_global_admin(session, viewer_id):
            if requested_side is not None:
                if requested_side not in (EntrySide.CONSULT, EntrySide.DEV):
                    raise Forbidden("总管理员只能以咨询或研发身份发送正式消息")
                return requested_side
            raise Forbidden("总管理员发送正式消息时必须明确选择咨询或研发身份")
        consult = self.is_member(session, user_id=viewer_id, team_id=case.consult_queue_id)
        dev = self.is_member(session, user_id=viewer_id, team_id=case.current_dev_team_id)
        allowed = {
            side
            for side, is_allowed in ((EntrySide.CONSULT, consult), (EntrySide.DEV, dev))
            if is_allowed
        }
        if requested_side is not None:
            if requested_side not in allowed:
                raise Forbidden("你不能以该身份向此事件发送正式消息")
            return requested_side
        if len(allowed) == 1:
            return next(iter(allowed))
        if not allowed:
            raise Forbidden("你没有向此事件发送正式消息的权限")
        raise Forbidden("你同时属于双方团队，请明确选择发送身份")

    def assert_consult_manager(self, session: Session, actor_id: UUID, case: Case) -> None:
        if self.is_global_admin(session, actor_id):
            return
        if not self.is_member(session, user_id=actor_id, team_id=case.consult_queue_id):
            raise Forbidden("你不属于该咨询队列")
        if actor_id == case.current_consultant_id or self.is_admin(
            session, user_id=actor_id, team_id=case.consult_queue_id
        ):
            return
        raise Forbidden("只有当前咨询经办人或咨询队列管理员可以执行此操作")

    def assert_developer_transfer_authorized(
        self, session: Session, actor_id: UUID, case: Case
    ) -> None:
        if self.is_global_admin(session, actor_id):
            return
        consult_current = actor_id == case.current_consultant_id and self.is_member(
            session, user_id=actor_id, team_id=case.consult_queue_id
        )
        developer_current = actor_id == case.current_developer_id and self.is_member(
            session, user_id=actor_id, team_id=case.current_dev_team_id
        )
        team_admin = self.is_admin(session, user_id=actor_id, team_id=case.current_dev_team_id)
        if consult_current or developer_current or team_admin:
            return
        raise Forbidden("只有当前经办人或研发团队管理员可以转交研发")

    def assert_developer_in_team(self, session: Session, developer_id: UUID, team_id: UUID) -> User:
        self.get_team(session, team_id, kind=TeamKind.DEV)
        developer = self.get_user(session, developer_id)
        if not self.is_member(session, user_id=developer_id, team_id=team_id):
            raise RoutingUnavailable("研发处理人不属于目标研发团队")
        return developer

    def assert_consultant_in_queue(
        self, session: Session, consultant_id: UUID, queue_id: UUID
    ) -> User:
        self.get_team(session, queue_id, kind=TeamKind.CONSULT_QUEUE)
        consultant = self.get_user(session, consultant_id)
        if self.is_global_admin(session, consultant_id):
            return consultant
        if not self.is_member(session, user_id=consultant_id, team_id=queue_id):
            raise RoutingUnavailable("咨询经办人不属于目标咨询队列")
        return consultant

    def active_teams_for_user(
        self, session: Session, *, user_id: UUID, kind: TeamKind
    ) -> list[Team]:
        if self.is_global_admin(session, user_id):
            return session.scalars(
                select(Team)
                .where(Team.kind == kind, Team.active.is_(True))
                .order_by(Team.name.asc())
            ).all()
        now = utc_now()
        valid_from, valid_until = self._current_membership_conditions(now)
        return session.scalars(
            select(Team)
            .join(TeamMembership, TeamMembership.team_id == Team.id)
            .where(
                TeamMembership.user_id == user_id,
                Team.kind == kind,
                Team.active.is_(True),
                valid_from,
                valid_until,
            )
            .order_by(Team.name.asc())
        ).all()

    def list_active_developers(
        self, session: Session, *, query: str = "", limit: int = 50
    ) -> list[User]:
        now = utc_now()
        valid_from, valid_until = self._current_membership_conditions(now)
        statement = (
            select(User)
            .join(TeamMembership, TeamMembership.user_id == User.id)
            .join(Team, Team.id == TeamMembership.team_id)
            .where(
                User.active.is_(True),
                Team.active.is_(True),
                Team.kind == TeamKind.DEV,
                valid_from,
                valid_until,
            )
            .distinct()
            .order_by(User.display_name.asc(), User.wecom_userid.asc())
            .limit(max(1, min(limit, 100)))
        )
        normalized = query.strip()
        if normalized:
            pattern = f"%{normalized}%"
            statement = statement.where(
                or_(User.display_name.ilike(pattern), User.wecom_userid.ilike(pattern))
            )
        return session.scalars(statement).all()


class ValidationErrorForDirectory(RoutingUnavailable):
    """A routing-specific invalid input that must remain fail-closed."""
