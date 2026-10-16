"""Resolve declared business identities to registered WeCom delivery addresses."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import Actor
from kefu.case_desk.errors import Forbidden, NotFound, RoutingUnavailable
from kefu.messages.contracts import PushTarget
from kefu.persistence.models import (
    DeliveryDestination,
    Team,
    TeamKind,
    TeamMembership,
    User,
    WeComChannel,
)


class RecipientKind(StrEnum):
    TEAM = "team"
    CONSULT_QUEUE = "consult_queue"
    PERSON = "person"


@dataclass(frozen=True, slots=True)
class RecipientReference:
    kind: RecipientKind
    identity: UUID


class RecipientResolver:
    """Turn stored team and person IDs into WeCom targets; missing mappings fail closed."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def resolve(self, recipient: RecipientReference) -> PushTarget:
        with self._session_factory() as session:
            if recipient.kind is RecipientKind.PERSON:
                user = session.get(User, recipient.identity)
                if user is None:
                    raise NotFound("接收人不存在")
                if not user.active or not user.wecom_userid.strip():
                    raise RoutingUnavailable("接收人的企业微信映射未启用")
                return PushTarget(DeliveryDestination.USER, user.wecom_userid)

            team = session.get(Team, recipient.identity)
            if team is None:
                raise NotFound("接收团队不存在")
            expected_kind = (
                TeamKind.CONSULT_QUEUE
                if recipient.kind is RecipientKind.CONSULT_QUEUE
                else TeamKind.DEV
            )
            if team.kind is not expected_kind:
                raise RoutingUnavailable("接收团队类型与目标不匹配")
            if not team.active:
                raise RoutingUnavailable("接收团队已停用")
            channels = session.scalars(
                select(WeComChannel).where(
                    WeComChannel.team_id == team.id,
                    WeComChannel.active.is_(True),
                )
            ).all()
            if len(channels) != 1 or not channels[0].chatid.strip():
                raise RoutingUnavailable("接收团队没有唯一的有效企微群聊绑定")
            return PushTarget(DeliveryDestination.CHAT, channels[0].chatid)

    def authorize(self, actor: Actor, recipient: RecipientReference) -> PushTarget:
        target = self.resolve(recipient)
        now = datetime.now(UTC)
        if recipient.kind is RecipientKind.PERSON:
            with self._session_factory() as session:
                user = session.get(User, recipient.identity)
                actor_user = session.get(User, actor.user_id)
                if user is None or not user.active or actor_user is None or not actor_user.active:
                    raise Forbidden("当前身份无权推送消息")
                if actor_user.is_global_admin or user.id == actor_user.id:
                    return target
                # A person push is allowed within a shared active team; arbitrary
                # enterprise IDs cannot be supplied to the tool.
                other_membership = TeamMembership.__table__.alias("other_membership")
                shared_team = session.scalar(
                    select(TeamMembership.team_id)
                    .join(
                        other_membership,
                        other_membership.c.team_id == TeamMembership.team_id,
                    )
                    .join(Team, Team.id == TeamMembership.team_id)
                    .where(
                        TeamMembership.user_id == actor.user_id,
                        other_membership.c.user_id == user.id,
                        Team.active.is_(True),
                        TeamMembership.valid_from <= now,
                        (TeamMembership.valid_until.is_(None)) | (TeamMembership.valid_until > now),
                        other_membership.c.valid_from <= now,
                        (other_membership.c.valid_until.is_(None))
                        | (other_membership.c.valid_until > now),
                    )
                    .limit(1)
                )
                if shared_team is None:
                    raise Forbidden("只能向本人或同一团队成员推送消息")
            return target

        with self._session_factory() as session:
            team = session.get(Team, recipient.identity)
            actor_user = session.get(User, actor.user_id)
            if team is None or actor_user is None or not actor_user.active:
                raise Forbidden("当前身份无权推送消息")
            if actor_user.is_global_admin:
                return target
            if (
                session.scalar(
                    select(TeamMembership.team_id)
                    .join(Team, Team.id == TeamMembership.team_id)
                    .where(
                        TeamMembership.user_id == actor.user_id,
                        TeamMembership.team_id == team.id,
                        TeamMembership.valid_from <= now,
                        (TeamMembership.valid_until.is_(None)) | (TeamMembership.valid_until > now),
                        Team.active.is_(True),
                    )
                )
                is None
            ):
                raise Forbidden("只能向本人所属团队推送消息")
        return target
