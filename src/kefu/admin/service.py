"""Business operations behind the H5 organization and authorization center.

The event desk deliberately owns event transitions, while this module owns
the smaller but still security-sensitive organization changes: identities,
teams, memberships, global administrators, and team chat bindings.
The web layer only translates forms/JSON into these operations.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import Actor
from kefu.case_desk.errors import Conflict, Forbidden, NotFound, ValidationError
from kefu.inspection.schedule import (
    DEFAULT_INSPECTION_TIMES,
    normalize_inspection_times,
)
from kefu.media.storage import MediaIngestError, ObjectStorage
from kefu.observability import opaque_id_fingerprint
from kefu.persistence.models import (
    Case,
    CaseEntry,
    CaseEntryPart,
    ContentDeletionRecord,
    DeferredPassiveReply,
    DeletionRecordOutcome,
    DeletionTargetKind,
    Delivery,
    DeliveryItem,
    GroupIssueIntakeSession,
    GroupIssueIntakeTurn,
    InspectionSchedule,
    IssueInspectionState,
    IssueIntakeDraft,
    IssueStatusUpdateJob,
    LifecycleStatus,
    MembershipRole,
    StoredMedia,
    Team,
    TeamKind,
    TeamMembership,
    User,
    WeComChannel,
)
from kefu.routing.directory import DatabaseRoutingDirectory, TeamChannel

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _issue_deletion_label(case: Case) -> str:
    return f"{case.case_ref} · {case.title}"


def _team_kind_label(kind: TeamKind) -> str:
    return "咨询团队" if kind is TeamKind.CONSULT_QUEUE else "研发团队"


def _image_ids(parts: object) -> set[UUID]:
    if not isinstance(parts, list):
        return set()
    result: set[UUID] = set()
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") != "image":
            continue
        media_id = part.get("media_id")
        if not isinstance(media_id, str):
            continue
        try:
            result.add(UUID(media_id))
        except ValueError:
            continue
    return result


def _draft_references_team(
    fields: object,
    consult_queue_id: UUID | None,
    dev_team_id: UUID | None,
) -> bool:
    if not isinstance(fields, dict):
        return False
    consult_reference = fields.get("consult_queue_id")
    dev_reference = fields.get("initial_acceptance_team_id")
    return (
        consult_queue_id is not None and consult_reference == str(consult_queue_id)
    ) or (dev_team_id is not None and dev_reference == str(dev_team_id))


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
    lead_display_name: str | None
    kind: TeamKind
    active: bool
    channel_chatid: str | None
    channel_initialized: bool
    inspection_age_hours: int | None
    inspection_own_only: bool
    members: tuple[ManagedTeamMember, ...]


@dataclass(frozen=True, slots=True)
class ManagedInspectionSchedule:
    times: tuple[str, ...]
    timezone: str = "Asia/Shanghai"


@dataclass(frozen=True, slots=True)
class DeletionResult:
    operation_id: UUID
    deleted_target_id: UUID
    deleted_target_label: str
    record_ids: tuple[UUID, ...]
    deleted_issue_numbers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContentDeletionQueueItem:
    id: UUID
    operation_id: UUID
    parent_record_id: UUID | None
    target_kind: DeletionTargetKind
    target_id: UUID | None
    target_label: str
    actor_name: str
    outcome: DeletionRecordOutcome
    error_message: str | None
    created_at: datetime


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
        storage: ObjectStorage | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._directory = directory or DatabaseRoutingDirectory()
        self._storage = storage

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

    def inspection_schedule(self, actor: Actor) -> ManagedInspectionSchedule:
        with self._session_factory() as session, session.begin():
            self._assert_admin(session, actor)
            schedule = session.get(InspectionSchedule, 1)
            if schedule is None:
                return ManagedInspectionSchedule(times=DEFAULT_INSPECTION_TIMES)
            times = normalize_inspection_times(schedule.times)
            return ManagedInspectionSchedule(times=times)

    def update_inspection_schedule(
        self, actor: Actor, *, times: object
    ) -> ManagedInspectionSchedule:
        normalized_times = normalize_inspection_times(times)
        with self._session_factory() as session, session.begin():
            self._assert_admin(session, actor)
            schedule = session.get(InspectionSchedule, 1)
            if schedule is None:
                schedule = InspectionSchedule(id=1, times=list(normalized_times))
                session.add(schedule)
            else:
                schedule.times = list(normalized_times)
            session.flush()
            return ManagedInspectionSchedule(times=normalized_times)

    def update_team_inspection_rule(
        self,
        actor: Actor,
        *,
        team_id: UUID,
        age_hours: int | None,
        own_only: bool,
    ) -> ManagedTeam:
        if age_hours is not None and (
            not isinstance(age_hours, int) or isinstance(age_hours, bool) or age_hours <= 0
        ):
            raise ValidationError("巡检小时数必须是正整数")
        if age_hours is not None and age_hours > 2_147_483_647:
            raise ValidationError("巡检小时数不能超过 2147483647")
        if not isinstance(own_only, bool):
            raise ValidationError("问题归属条件必须是布尔值")
        with self._session_factory() as session, session.begin():
            self._assert_admin(session, actor)
            team = session.get(Team, team_id)
            if team is None:
                raise NotFound("团队不存在")
            team.inspection_age_hours = age_hours
            team.inspection_own_only = own_only
            session.flush()
            return self._team_view(session, team)

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

    def create_team(
        self,
        actor: Actor,
        *,
        kind: TeamKind,
        name: str,
        lead_display_name: str | None = None,
    ) -> ManagedTeam:
        try:
            kind = TeamKind(kind)
        except (TypeError, ValueError) as error:
            raise ValidationError("团队类型只能是咨询队列或研发责任团队") from error
        normalized_name = _team_name(name)
        normalized_lead_name = (
            _team_lead_display_name(lead_display_name) if kind is TeamKind.DEV else None
        )
        with self._session_factory() as session, session.begin():
            self._assert_admin(session, actor)
            existing = session.scalar(
                select(Team).where(Team.kind == kind, Team.name == normalized_name)
            )
            if existing is not None:
                raise Conflict("同类型团队名称已经存在")
            team = Team(
                id=uuid4(),
                kind=kind,
                name=normalized_name,
                lead_display_name=normalized_lead_name,
                active=True,
            )
            session.add(team)
            session.flush()
            return self._team_view(session, team)

    def update_team(
        self,
        actor: Actor,
        team_id: UUID,
        *,
        name: str | None = None,
        lead_display_name: str | None = None,
        active: bool | None = None,
    ) -> ManagedTeam:
        if name is not None:
            name = _team_name(name)
        if lead_display_name is not None:
            lead_display_name = _team_lead_display_name(lead_display_name)
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
            if lead_display_name is not None:
                if team.kind is not TeamKind.DEV:
                    raise ValidationError("只有研发团队可以设置负责人展示名")
                team.lead_display_name = lead_display_name
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
            channel = self._directory.bind_team_chat(
                session,
                team_name=team.name,
                chatid=normalized_chatid,
                team_kind=team.kind,
            )
            team_kind = team.kind
            team_name = team.name
        logger.info(
            "[GROUP-TRACE] h5_binding_committed chatid_fp=%s team_id=%s team_kind=%s team_name=%r",
            opaque_id_fingerprint(channel.chatid),
            channel.team_id,
            team_kind,
            team_name,
        )
        return channel

    def delete_closed_issue(self, actor: Actor, issue_number: str) -> DeletionResult:
        """Hard-delete one closed issue and persist its queue record atomically."""
        operation_id = uuid4()
        target_id: UUID | None = None
        target_label = issue_number.strip() or "未知问题"
        try:
            with self._session_factory() as session, session.begin():
                admin = self._assert_admin(session, actor)
                case = session.scalar(
                    select(Case)
                    .where(Case.case_ref == issue_number.strip())
                    .with_for_update()
                )
                if case is None:
                    raise NotFound("问题不存在")
                target_id = case.id
                target_label = _issue_deletion_label(case)
                issue_ref = case.case_ref
                if case.lifecycle_status is not LifecycleStatus.CLOSED:
                    raise Conflict("只有已关闭问题可以删除")
                self._assert_no_surviving_related_issues(session, (case.id,))
                self._delete_cases(session, (case,))
                record = self._new_deletion_record(
                    operation_id=operation_id,
                    target_kind=DeletionTargetKind.ISSUE,
                    target_id=case.id,
                    target_label=target_label,
                    actor=admin,
                    outcome=DeletionRecordOutcome.SUCCEEDED,
                )
                session.add(record)
                session.flush()
                result = DeletionResult(
                    operation_id=operation_id,
                    deleted_target_id=case.id,
                    deleted_target_label=target_label,
                    record_ids=(record.id,),
                    deleted_issue_numbers=(issue_ref,),
                )
            return result
        except Exception as error:
            self._record_failed_deletion(
                actor=actor,
                operation_id=operation_id,
                target_kind=DeletionTargetKind.ISSUE,
                target_id=target_id,
                target_label=target_label,
                error=error,
            )
            raise

    def delete_team(self, actor: Actor, team_id: UUID) -> DeletionResult:
        """Delete a team and its closed issues, refusing any live references."""
        operation_id = uuid4()
        target_label = str(team_id)
        try:
            with self._session_factory() as session, session.begin():
                admin = self._assert_admin(session, actor)
                team = session.scalar(
                    select(Team).where(Team.id == team_id).with_for_update()
                )
                if team is None:
                    raise NotFound("团队不存在")
                target_label = f"{team.name}（{_team_kind_label(team.kind)}）"
                related_cases = tuple(
                    session.scalars(
                        select(Case)
                        .where(
                            or_(
                                Case.consult_queue_id == team.id,
                                Case.current_dev_team_id == team.id,
                                Case.initial_acceptance_team_id == team.id,
                            )
                        )
                        .order_by(Case.created_at, Case.id)
                        .with_for_update()
                    ).all()
                )
                open_cases = tuple(
                    case
                    for case in related_cases
                    if case.lifecycle_status is not LifecycleStatus.CLOSED
                )
                if open_cases:
                    issue_refs = "、".join(case.case_ref for case in open_cases[:5])
                    more = "等" if len(open_cases) > 5 else ""
                    raise Conflict(
                        f"团队仍被未关闭问题引用，不能删除：{issue_refs}{more}"
                    )
                case_ids = tuple(case.id for case in related_cases)
                issue_numbers = tuple(case.case_ref for case in related_cases)
                issue_labels = {case.id: _issue_deletion_label(case) for case in related_cases}
                self._assert_no_surviving_related_issues(session, case_ids)

                team_record = self._new_deletion_record(
                    operation_id=operation_id,
                    target_kind=DeletionTargetKind.TEAM,
                    target_id=team.id,
                    target_label=target_label,
                    actor=admin,
                    outcome=DeletionRecordOutcome.SUCCEEDED,
                )
                session.add(team_record)
                self._delete_cases(
                    session,
                    related_cases,
                    consult_queue_id=(
                        team.id if team.kind is TeamKind.CONSULT_QUEUE else None
                    ),
                    dev_team_id=team.id if team.kind is TeamKind.DEV else None,
                )
                session.execute(
                    delete(TeamMembership).where(TeamMembership.team_id == team.id)
                )
                session.execute(delete(WeComChannel).where(WeComChannel.team_id == team.id))
                session.delete(team)
                case_records = tuple(
                    self._new_deletion_record(
                        operation_id=operation_id,
                        parent_record_id=team_record.id,
                        target_kind=DeletionTargetKind.ISSUE,
                        target_id=case.id,
                        target_label=issue_labels[case.id],
                        actor=admin,
                        outcome=DeletionRecordOutcome.SUCCEEDED,
                    )
                    for case in related_cases
                )
                session.add_all(case_records)
                session.flush()
                result = DeletionResult(
                    operation_id=operation_id,
                    deleted_target_id=team.id,
                    deleted_target_label=target_label,
                    record_ids=(team_record.id, *(record.id for record in case_records)),
                    deleted_issue_numbers=issue_numbers,
                )
            return result
        except Exception as error:
            self._record_failed_deletion(
                actor=actor,
                operation_id=operation_id,
                target_kind=DeletionTargetKind.TEAM,
                target_id=team_id,
                target_label=target_label,
                error=error,
            )
            raise

    def list_content_deletions(
        self, actor: Actor, *, limit: int = 50
    ) -> tuple[ContentDeletionQueueItem, ...]:
        page_size = max(1, min(limit, 50))
        with self._session_factory() as session:
            self._assert_admin(session, actor)
            rows = session.scalars(
                select(ContentDeletionRecord)
                .order_by(ContentDeletionRecord.created_at.desc(), ContentDeletionRecord.id)
                .limit(page_size)
            ).all()
            return tuple(
                ContentDeletionQueueItem(
                    id=row.id,
                    operation_id=row.operation_id,
                    parent_record_id=row.parent_record_id,
                    target_kind=row.target_kind,
                    target_id=row.target_id,
                    target_label=row.target_label,
                    actor_name=row.actor_name,
                    outcome=row.outcome,
                    error_message=row.error_message,
                    created_at=_as_utc(row.created_at),
                )
                for row in rows
            )

    def _assert_no_surviving_related_issues(
        self, session: Session, target_case_ids: tuple[UUID, ...]
    ) -> None:
        if not target_case_ids:
            return
        target_set = set(target_case_ids)
        dependents = session.scalars(
            select(Case).where(Case.related_case_id.in_(target_case_ids))
        ).all()
        surviving = tuple(case for case in dependents if case.id not in target_set)
        if surviving:
            issue_refs = "、".join(case.case_ref for case in surviving[:5])
            more = "等" if len(surviving) > 5 else ""
            raise Conflict(f"问题仍被其他问题关联，不能删除：{issue_refs}{more}")

    def _delete_cases(
        self,
        session: Session,
        cases: tuple[Case, ...],
        *,
        consult_queue_id: UUID | None = None,
        dev_team_id: UUID | None = None,
    ) -> None:
        case_ids = tuple(case.id for case in cases)
        case_refs = tuple(case.case_ref for case in cases)

        entries = (
            tuple(
                session.scalars(select(CaseEntry).where(CaseEntry.case_id.in_(case_ids))).all()
            )
            if case_ids
            else ()
        )
        entry_ids = tuple(entry.id for entry in entries)
        media_ids: set[UUID] = set()
        if entry_ids:
            media_ids.update(
                media_id
                for media_id in session.scalars(
                    select(CaseEntryPart.media_id).where(
                        CaseEntryPart.entry_id.in_(entry_ids), CaseEntryPart.media_id.is_not(None)
                    )
                )
                if media_id is not None
            )

        session_statement = select(GroupIssueIntakeSession)
        session_conditions = []
        if case_ids:
            session_conditions.append(GroupIssueIntakeSession.issue_id.in_(case_ids))
        if consult_queue_id is not None:
            session_conditions.append(
                GroupIssueIntakeSession.consult_queue_id == consult_queue_id
            )
        intake_sessions = (
            tuple(session.scalars(session_statement.where(or_(*session_conditions))).all())
            if session_conditions
            else ()
        )
        intake_session_ids = tuple(record.id for record in intake_sessions)
        for record in intake_sessions:
            media_ids.update(_image_ids(record.parts_json))

        drafts = tuple(
            session.scalars(
                select(IssueIntakeDraft).where(
                    IssueIntakeDraft.confirmed_issue_id.in_(case_ids)
                )
            ).all()
        ) if case_ids else ()
        if consult_queue_id is not None or dev_team_id is not None:
            unconfirmed = session.scalars(
                select(IssueIntakeDraft).where(IssueIntakeDraft.confirmed_issue_id.is_(None))
            ).all()
            for record in unconfirmed:
                if _draft_references_team(record.fields_json, consult_queue_id, dev_team_id):
                    drafts += (record,)
        for record in drafts:
            media_ids.update(_image_ids(record.parts_json))

        if intake_session_ids:
            session.execute(
                delete(GroupIssueIntakeTurn).where(
                    GroupIssueIntakeTurn.session_id.in_(intake_session_ids)
                )
            )
            session.execute(
                delete(GroupIssueIntakeSession).where(
                    GroupIssueIntakeSession.id.in_(intake_session_ids)
                )
            )
        if drafts:
            session.execute(
                delete(IssueIntakeDraft).where(
                    IssueIntakeDraft.id.in_(tuple(record.id for record in drafts))
                )
            )
        if case_refs:
            session.execute(
                delete(DeferredPassiveReply).where(
                    DeferredPassiveReply.case_ref.in_(case_refs)
                )
            )
        if case_ids:
            session.execute(
                delete(IssueStatusUpdateJob).where(IssueStatusUpdateJob.case_id.in_(case_ids))
            )
            session.execute(
                delete(IssueInspectionState).where(IssueInspectionState.issue_id.in_(case_ids))
            )
            if entry_ids:
                delivery_ids = tuple(
                    session.scalars(
                        select(Delivery.id).where(Delivery.entry_id.in_(entry_ids))
                    ).all()
                )
                if delivery_ids:
                    session.execute(
                        delete(DeliveryItem).where(DeliveryItem.delivery_id.in_(delivery_ids))
                    )
                    session.execute(delete(Delivery).where(Delivery.id.in_(delivery_ids)))
                session.execute(
                    delete(CaseEntryPart).where(CaseEntryPart.entry_id.in_(entry_ids))
                )
                session.execute(
                    update(CaseEntry)
                    .where(CaseEntry.id.in_(entry_ids))
                    .values(corrects_entry_id=None)
                )
                session.execute(delete(CaseEntry).where(CaseEntry.id.in_(entry_ids)))
            session.execute(
                update(Case)
                .where(Case.id.in_(case_ids))
                .values(related_case_id=None)
            )
            session.execute(delete(Case).where(Case.id.in_(case_ids)))
        session.flush()

        # Unconfirmed drafts and group intake sessions store image IDs in JSON,
        # so preserve an object unless no live row still references it.
        retained_media_ids: set[UUID] = set()
        for parts_json in session.scalars(select(IssueIntakeDraft.parts_json)):
            retained_media_ids.update(_image_ids(parts_json))
        for parts_json in session.scalars(select(GroupIssueIntakeSession.parts_json)):
            retained_media_ids.update(_image_ids(parts_json))
        retained_media_ids.update(
            media_id
            for media_id in session.scalars(
                select(CaseEntryPart.media_id).where(CaseEntryPart.media_id.is_not(None))
            )
            if media_id is not None
        )

        for media_id in media_ids - retained_media_ids:
            media = session.get(StoredMedia, media_id)
            if media is None:
                continue
            if self._storage is None:
                raise MediaIngestError("未配置对象存储，无法删除问题附件")
            self._storage.delete_prefix(media.object_key)
            session.delete(media)

    def _new_deletion_record(
        self,
        *,
        operation_id: UUID,
        target_kind: DeletionTargetKind,
        target_id: UUID | None,
        target_label: str,
        actor: User,
        outcome: DeletionRecordOutcome,
        parent_record_id: UUID | None = None,
        error_message: str | None = None,
    ) -> ContentDeletionRecord:
        return ContentDeletionRecord(
            id=uuid4(),
            operation_id=operation_id,
            parent_record_id=parent_record_id,
            target_kind=target_kind,
            target_id=target_id,
            target_label=target_label[:768],
            actor_user_id=actor.id,
            actor_name=actor.display_name,
            outcome=outcome,
            error_message=error_message[:1000] if error_message else None,
            created_at=utc_now(),
        )

    def _record_failed_deletion(
        self,
        *,
        actor: Actor,
        operation_id: UUID,
        target_kind: DeletionTargetKind,
        target_id: UUID | None,
        target_label: str,
        error: Exception,
    ) -> None:
        try:
            with self._session_factory() as session, session.begin():
                admin = self._assert_admin(session, actor)
                session.add(
                    self._new_deletion_record(
                        operation_id=operation_id,
                        target_kind=target_kind,
                        target_id=target_id,
                        target_label=target_label,
                        actor=admin,
                        outcome=DeletionRecordOutcome.FAILED,
                        error_message=str(error).replace("\n", " "),
                    )
                )
        except Forbidden:
            return
        except Exception:
            logger.exception(
                "content_deletion_failure_record_failed operation_id=%s", operation_id
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
            lead_display_name=team.lead_display_name,
            kind=team.kind,
            active=team.active,
            channel_chatid=channel.chatid if channel is not None else None,
            channel_initialized=channel is not None and channel.initialized_at is not None,
            inspection_age_hours=team.inspection_age_hours,
            inspection_own_only=team.inspection_own_only,
            members=members,
        )


def initialize_global_admin(
    session_factory: Callable[[], Session],
    *,
    wecom_userid: str,
    display_name: str = "问题中心总管理员",
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


def _team_lead_display_name(name: str | None) -> str:
    if not isinstance(name, str):
        raise ValidationError("研发团队负责人展示名不能为空")
    normalized_name = name.strip()
    if not normalized_name:
        raise ValidationError("研发团队负责人展示名不能为空")
    if len(normalized_name) > 256:
        raise ValidationError("研发团队负责人展示名不能超过 256 个字符")
    return normalized_name
