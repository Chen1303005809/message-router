"""The CaseDesk deep module.

All human and transport-facing code reaches event state through ``execute``.
The service owns authorization, append-only timeline writes, optimistic
versions, state transitions, and durable delivery records in one transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy import case as sql_case
from sqlalchemy.orm import Session, aliased

from kefu.case_desk.contracts import (
    SYSTEM_ACTOR,
    Actor,
    AdjustCaseDeadline,
    CaseFilter,
    CaseOverview,
    CaseSummary,
    CaseView,
    CloseCase,
    Command,
    CommandResult,
    ContentPart,
    CorrectEntry,
    CreateCase,
    DeliveryItemFailed,
    DeliveryItemSucceeded,
    DeliveryView,
    DeliveryWork,
    DeliveryWorkItem,
    EntryView,
    ExtendCaseDeadline,
    GroupActor,
    ImagePart,
    IssueDraftProcessingQueueItem,
    IssueStatusQueueItem,
    IssueStatusUpdateClaim,
    IssueStatusUpdateWork,
    MediaAccess,
    Page,
    PartView,
    PostFormalMessage,
    RecentIssueStatusChange,
    ReopenCase,
    SetCaseApproachingWindow,
    SetCaseDeadline,
    SetCaseStatus,
    SetIssueCurrentStatus,
    SystemActor,
    TeamOption,
    TextPart,
    TransferConsultant,
    TransferDevTeam,
    UpdateCaseMetadata,
    UserOption,
)
from kefu.case_desk.errors import Conflict, Forbidden, NotFound, RoutingUnavailable, ValidationError
from kefu.case_desk.markers import new_case_ref, normalize_case_ref
from kefu.case_desk.numbering import next_bug_number
from kefu.persistence.models import (
    DEFAULT_APPROACHING_WINDOW_MINUTES,
    DEFAULT_CASE_DEADLINE_HOURS,
    MAX_DEADLINE_INCREMENT_MINUTES,
    MIN_DEADLINE_INCREMENT_MINUTES,
    Case,
    CaseEntry,
    CaseEntryKind,
    CaseEntryPart,
    CaseStatus,
    DeadlineStatus,
    Delivery,
    DeliveryDestination,
    DeliveryItem,
    DeliveryItemStatus,
    DeliveryStatus,
    EntrySide,
    IssueCurrentStatus,
    IssueDraftProcessingTask,
    IssueIntakeDraft,
    IssueModule,
    IssueStatusUpdateJob,
    IssueStatusUpdateState,
    LifecycleStatus,
    MessageIntent,
    PartKind,
    StoredMedia,
    Team,
    TeamKind,
    User,
    WaitingOn,
)
from kefu.routing.directory import DatabaseRoutingDirectory, TeamChannel
from kefu.wecom.formatting import SourcePart, build_formal_bundle, build_notice_bundle

RETRY_DELAYS = (timedelta(seconds=5), timedelta(seconds=30), timedelta(minutes=2))
DEFAULT_DELIVERY_LEASE = timedelta(seconds=60)
DEFAULT_WEB_BASE_URL = "http://localhost:8000"
DEFERRED_PASSIVE_PENDING_MODE = "deferred_passive_reply_pending"
DEFERRED_PASSIVE_COMPLETED_MODE = "deferred_passive_reply"
STATUS_UPDATE_RETRY_DELAYS = (
    timedelta(seconds=5),
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=5),
)
DEFAULT_STATUS_UPDATE_LEASE = timedelta(seconds=90)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _clean_optional_text(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _original_speaker_name(entry: CaseEntry) -> str:
    original_name = entry.metadata_json.get("original_actor_name")
    if isinstance(original_name, str) and original_name.strip():
        return original_name.strip()
    return entry.actor_name_snapshot


def _case_customer_group_name(case: Case) -> str:
    return case.customer_group_name.strip() if case.customer_group_name else ""


def _case_deadline_status(case: Case, now: datetime | None = None) -> DeadlineStatus | None:
    if case.lifecycle_status is LifecycleStatus.CLOSED:
        return None
    current_time = _as_utc(now or utc_now())
    deadline = _as_utc(case.deadline)
    if deadline <= current_time:
        return DeadlineStatus.OVERDUE
    if deadline <= current_time + timedelta(minutes=case.approaching_window_minutes):
        return DeadlineStatus.APPROACHING
    return None


def _validated_deadline_minutes(value: int, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < MIN_DEADLINE_INCREMENT_MINUTES
        or value > MAX_DEADLINE_INCREMENT_MINUTES
        or value % MIN_DEADLINE_INCREMENT_MINUTES != 0
    ):
        raise ValidationError(f"{label}必须在30分钟到可设置上限之间，并按30分钟递增")
    return value


def _validated_deadline_adjustment_minutes(value: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value == 0
        or abs(value) > MAX_DEADLINE_INCREMENT_MINUTES
        or abs(value) % MIN_DEADLINE_INCREMENT_MINUTES != 0
    ):
        raise ValidationError("调整时长绝对值必须在30分钟到可设置上限之间，并按30分钟递增")
    return value


class CaseDesk:
    """Event service with the public shape ``execute/get_case/list_cases``.

    ``claim_pending_deliveries`` is intentionally an internal worker-facing
    operation.  It only returns already-persisted payloads and cannot invent or
    mutate business state on its own.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        directory: DatabaseRoutingDirectory | None = None,
        web_base_url: str = DEFAULT_WEB_BASE_URL,
    ) -> None:
        self._session_factory = session_factory
        self._directory = directory or DatabaseRoutingDirectory()
        self._web_base_url = web_base_url.strip().rstrip("/")
        if not self._web_base_url:
            raise ValueError("web_base_url 不能为空")

    def execute(self, command: Command, actor: Actor | GroupActor | SystemActor) -> CommandResult:
        """Apply a closed command set atomically and return its stable result."""
        with self._session_factory() as session, session.begin():
            if isinstance(command, (DeliveryItemSucceeded, DeliveryItemFailed)):
                if actor is not SYSTEM_ACTOR:
                    raise Forbidden("投递结果只能由系统记录")
                if isinstance(command, DeliveryItemSucceeded):
                    return self._delivery_item_succeeded(session, command)
                return self._delivery_item_failed(session, command)

            if isinstance(command, PostFormalMessage) and isinstance(actor, GroupActor):
                return self._post_formal_message(session, command, actor)
            if isinstance(command, CreateCase) and isinstance(actor, GroupActor):
                return self._create_case(session, command, actor)
            if not isinstance(actor, Actor):
                raise Forbidden("此操作需要已认证的人员身份")
            if isinstance(command, CreateCase):
                return self._create_case(session, command, actor)
            if isinstance(command, (AdjustCaseDeadline, ExtendCaseDeadline)):
                return self._adjust_case_deadline(session, command, actor)
            if isinstance(command, SetCaseDeadline):
                return self._set_case_deadline(session, command, actor)
            if isinstance(command, SetCaseApproachingWindow):
                return self._set_case_approaching_window(session, command, actor)
            if isinstance(command, PostFormalMessage):
                return self._post_formal_message(session, command, actor)
            if isinstance(command, UpdateCaseMetadata):
                return self._update_case_metadata(session, command, actor)
            if isinstance(command, SetIssueCurrentStatus):
                return self._set_issue_current_status(session, command, actor)
            if isinstance(command, TransferConsultant):
                return self._transfer_consultant(session, command, actor)
            if isinstance(command, TransferDevTeam):
                return self._transfer_dev_team(session, command, actor)
            if isinstance(command, CloseCase):
                return self._close_case(session, command, actor)
            if isinstance(command, SetCaseStatus):
                return self._set_case_status(session, command, actor)
            if isinstance(command, ReopenCase):
                return self._reopen_case(session, command, actor)
            if isinstance(command, CorrectEntry):
                return self._correct_entry(session, command, actor)
            raise ValidationError(f"不支持的问题命令：{type(command).__name__}")

    def get_case(self, case_ref: str, viewer: Actor) -> CaseView:
        """Return a fully authorized, ordered event timeline."""
        with self._session_factory() as session:
            case = self._load_case(session, case_ref)
            self._directory.assert_case_visible(session, viewer.user_id, case)
            try:
                self._directory.assert_consult_manager(session, viewer.user_id, case)
                can_edit_metadata = True
                can_change_consult_status = True
            except Forbidden:
                can_edit_metadata = False
                can_change_consult_status = False
            can_accept_pending = self._directory.is_member(
                session, user_id=viewer.user_id, team_id=case.current_dev_team_id
            )
            can_extend_deadline = (
                case.lifecycle_status is LifecycleStatus.OPEN
                and self._has_consult_deadline_access(session, viewer.user_id, case)
            )
            can_transfer = self._directory.can_transfer_case(session, viewer.user_id, case)
            transferable_consultants = (
                tuple(
                    UserOption(
                        id=user.id,
                        display_name=user.display_name,
                        wecom_userid=user.wecom_userid,
                    )
                    for user in self._directory.active_members_in_team(
                        session,
                        team_id=case.consult_queue_id,
                        team_kind=TeamKind.CONSULT_QUEUE,
                    )
                )
                if can_transfer
                else ()
            )
            can_transfer_dev_team = self._directory.can_transfer_dev_team(
                session, viewer.user_id, case
            )
            transferable_dev_teams = (
                tuple(
                    TeamOption(
                        id=team.id,
                        name=team.name,
                        lead_display_name=team.lead_display_name,
                    )
                    for team in self._directory.active_dev_teams(session)
                    if team.id != case.current_dev_team_id
                )
                if can_transfer_dev_team
                else ()
            )
            return self._case_view(
                session,
                case,
                can_edit_metadata=can_edit_metadata,
                can_change_consult_status=can_change_consult_status,
                can_accept_pending=can_accept_pending,
                can_extend_deadline=can_extend_deadline,
                can_transfer=can_transfer,
                transferable_consultants=transferable_consultants,
                can_transfer_dev_team=can_transfer_dev_team,
                transferable_dev_teams=transferable_dev_teams,
            )

    def list_cases(self, case_filter: CaseFilter, viewer: Actor) -> Page:
        """List only events visible through the viewer's active team membership."""
        page_size = max(1, min(case_filter.page_size, 100))
        offset = max(0, case_filter.offset)
        now = utc_now()
        with self._session_factory() as session:
            visibility_filter, team_ids = self._case_visibility_filter(session, viewer, now)
            statement = select(Case)
            if visibility_filter is not None:
                statement = statement.where(visibility_filter)
            if case_filter.lifecycle_status is not None:
                statement = statement.where(Case.lifecycle_status == case_filter.lifecycle_status)
            if case_filter.waiting_on is not None:
                statement = statement.where(Case.waiting_on == case_filter.waiting_on)
            if case_filter.priority is not None:
                statement = statement.where(Case.priority == case_filter.priority)
            if case_filter.waiting_for_me and team_ids is not None:
                statement = statement.where(
                    or_(
                        and_(
                            Case.waiting_on == WaitingOn.CONSULT,
                            Case.consult_queue_id.in_(team_ids),
                        ),
                        and_(
                            Case.waiting_on == WaitingOn.DEV,
                            Case.current_dev_team_id.in_(team_ids),
                        ),
                    )
                )
            if case_filter.assigned_to_me:
                statement = statement.where(Case.current_consultant_id == viewer.user_id)
            cases = session.scalars(
                statement.order_by(Case.updated_at.desc(), Case.case_ref.asc())
                .offset(offset)
                .limit(page_size + 1)
            ).all()
            has_more = len(cases) > page_size
            visible_cases = cases[:page_size]
            developer_team_ids = self._directory.active_team_ids(
                session,
                user_id=viewer.user_id,
                team_kind=TeamKind.DEV,
                now=now,
            )
            return Page(
                items=tuple(self._case_summary(case, developer_team_ids) for case in visible_cases),
                offset=offset,
                next_offset=offset + page_size if has_more else None,
            )

    def overview(self, viewer: Actor) -> CaseOverview:
        """Count visible cases by lifecycle and current waiting side."""
        now = utc_now()
        with self._session_factory() as session:
            visibility_filter, _ = self._case_visibility_filter(session, viewer, now)
            statement = select(
                func.count(
                    sql_case((Case.lifecycle_status == LifecycleStatus.OPEN, Case.id))
                ).label("open_count"),
                func.count(
                    sql_case(
                        (
                            (Case.lifecycle_status == LifecycleStatus.OPEN)
                            & (Case.waiting_on == WaitingOn.CONSULT),
                            Case.id,
                        )
                    )
                ).label("waiting_consult_count"),
                func.count(
                    sql_case(
                        (
                            (Case.lifecycle_status == LifecycleStatus.OPEN)
                            & (Case.waiting_on == WaitingOn.DEV),
                            Case.id,
                        )
                    )
                ).label("waiting_dev_count"),
                func.count(
                    sql_case((Case.lifecycle_status == LifecycleStatus.CLOSED, Case.id))
                ).label("closed_count"),
            )
            if visibility_filter is not None:
                statement = statement.where(visibility_filter)
            counts = session.execute(statement).one()
            return CaseOverview(
                open_count=counts.open_count,
                waiting_consult_count=counts.waiting_consult_count,
                waiting_dev_count=counts.waiting_dev_count,
                closed_count=counts.closed_count,
            )

    def _case_visibility_filter(self, session: Session, viewer: Actor, now: datetime):
        """Return the same active-team scope used by both lists and overviews."""
        self._directory.get_user(session, viewer.user_id)
        from kefu.persistence.models import TeamMembership

        if self._directory.is_global_admin(session, viewer.user_id):
            return None, None
        team_ids = select(TeamMembership.team_id).where(
            TeamMembership.user_id == viewer.user_id,
            TeamMembership.valid_from <= now,
            or_(
                TeamMembership.valid_until.is_(None),
                TeamMembership.valid_until > now,
            ),
        )
        return (
            or_(
                Case.consult_queue_id.in_(team_ids),
                Case.current_dev_team_id.in_(team_ids),
            ),
            team_ids,
        )

    def list_consult_queues(self, viewer: Actor) -> tuple[TeamOption, ...]:
        with self._session_factory() as session:
            self._directory.get_user(session, viewer.user_id)
            teams = self._directory.active_teams_for_user(
                session, user_id=viewer.user_id, kind=TeamKind.CONSULT_QUEUE
            )
            routable: list[TeamOption] = []
            for team in teams:
                try:
                    self._directory.active_consult_channel(session, team.id)
                except RoutingUnavailable:
                    continue
                routable.append(TeamOption(id=team.id, name=team.name))
            return tuple(routable)

    def list_dev_teams(self, viewer: Actor) -> tuple[TeamOption, ...]:
        """List routable development teams, including their display-only lead labels."""
        with self._session_factory() as session:
            self._directory.get_user(session, viewer.user_id)
            teams = self._directory.active_dev_teams(session)
            return tuple(
                TeamOption(
                    id=team.id,
                    name=team.name,
                    lead_display_name=team.lead_display_name,
                )
                for team in teams
            )

    def get_media_access(self, case_ref: str, media_id: UUID, viewer: Actor) -> MediaAccess:
        """Authorize attachment retrieval through the same event scope as H5."""
        with self._session_factory() as session:
            case = self._load_case(session, case_ref)
            self._directory.assert_case_visible(session, viewer.user_id, case)
            media = session.scalar(
                select(StoredMedia)
                .join(CaseEntryPart, CaseEntryPart.media_id == StoredMedia.id)
                .join(CaseEntry, CaseEntry.id == CaseEntryPart.entry_id)
                .where(CaseEntry.case_id == case.id, StoredMedia.id == media_id)
            )
            if media is None:
                raise NotFound("该图片不属于此问题")
            return MediaAccess(id=media.id, object_key=media.object_key, mime_type=media.mime_type)

    def claim_pending_deliveries(
        self, limit: int, *, lease: timedelta = DEFAULT_DELIVERY_LEASE
    ) -> tuple[DeliveryWork, ...]:
        """Lease pending bundles for one worker without exposing ORM records.

        PostgreSQL honors ``SKIP LOCKED`` for concurrent workers.  SQLite ignores
        the clause in test mode but still exercises the same persisted workflow.
        """
        if limit < 1:
            raise ValidationError("投递批次大小必须大于零")
        now = utc_now()
        with self._session_factory() as session, session.begin():
            rows = session.execute(
                select(Delivery, CaseEntry, Case)
                .join(CaseEntry, CaseEntry.id == Delivery.entry_id)
                .join(Case, Case.id == CaseEntry.case_id)
                .where(
                    Delivery.status == DeliveryStatus.PENDING,
                    Delivery.next_attempt_at <= now,
                    or_(Delivery.locked_until.is_(None), Delivery.locked_until < now),
                )
                .order_by(Delivery.next_attempt_at.asc(), Delivery.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            work: list[DeliveryWork] = []
            for delivery, entry, case in rows:
                token = uuid4()
                delivery.lock_token = token
                delivery.locked_until = now + lease
                items = session.scalars(
                    select(DeliveryItem)
                    .where(
                        DeliveryItem.delivery_id == delivery.id,
                        DeliveryItem.status == DeliveryItemStatus.PENDING,
                    )
                    .order_by(DeliveryItem.position.asc())
                ).all()
                work.append(
                    DeliveryWork(
                        id=delivery.id,
                        lock_token=token,
                        destination_type=delivery.destination_type,
                        destination_address=delivery.destination_address,
                        case_ref=case.case_ref,
                        entry_id=entry.id,
                        items=tuple(
                            DeliveryWorkItem(
                                id=item.id,
                                req_id=item.req_id,
                                position=item.position,
                                kind=item.kind,
                                payload=cast(Mapping[str, object], item.payload_json),
                                status=item.status,
                            )
                            for item in items
                        ),
                    )
                )
            return tuple(work)

    def claim_pending_issue_status_updates(
        self,
        limit: int = 10,
        *,
        lease: timedelta = DEFAULT_STATUS_UPDATE_LEASE,
    ) -> tuple[IssueStatusUpdateWork, ...]:
        """Lease all currently queued status evaluations for each selected issue."""
        if limit < 1:
            raise ValidationError("状态判定批次大小必须大于零")
        now = utc_now()
        earlier_job = aliased(IssueStatusUpdateJob)
        has_earlier_job = (
            select(earlier_job.id)
            .where(
                earlier_job.case_id == IssueStatusUpdateJob.case_id,
                earlier_job.entry_sequence < IssueStatusUpdateJob.entry_sequence,
                earlier_job.state.in_(
                    (IssueStatusUpdateState.PENDING, IssueStatusUpdateState.PROCESSING)
                ),
            )
            .exists()
        )
        with self._session_factory() as session, session.begin():
            rows = session.execute(
                select(IssueStatusUpdateJob, Case)
                .join(Case, Case.id == IssueStatusUpdateJob.case_id)
                .where(
                    or_(
                        and_(
                            IssueStatusUpdateJob.state == IssueStatusUpdateState.PENDING,
                            IssueStatusUpdateJob.next_attempt_at <= now,
                            or_(
                                IssueStatusUpdateJob.locked_until.is_(None),
                                IssueStatusUpdateJob.locked_until < now,
                            ),
                        ),
                        and_(
                            IssueStatusUpdateJob.state == IssueStatusUpdateState.PROCESSING,
                            or_(
                                IssueStatusUpdateJob.locked_until.is_(None),
                                IssueStatusUpdateJob.locked_until < now,
                            ),
                        ),
                    ),
                    ~has_earlier_job,
                )
                .order_by(
                    IssueStatusUpdateJob.next_attempt_at.asc(),
                    IssueStatusUpdateJob.created_at.asc(),
                )
                .limit(limit)
                .with_for_update(of=IssueStatusUpdateJob, skip_locked=True)
            ).all()
            work: list[IssueStatusUpdateWork] = []
            claimed_case_ids: set[UUID] = set()
            for _job, case in rows:
                if case.id in claimed_case_ids:
                    continue
                claimed_case_ids.add(case.id)
                active_lease = session.scalar(
                    select(IssueStatusUpdateJob.id)
                    .where(
                        IssueStatusUpdateJob.case_id == case.id,
                        IssueStatusUpdateJob.state == IssueStatusUpdateState.PROCESSING,
                        IssueStatusUpdateJob.locked_until >= now,
                    )
                    .limit(1)
                )
                if active_lease is not None:
                    continue
                active_jobs = session.scalars(
                    select(IssueStatusUpdateJob)
                    .where(
                        IssueStatusUpdateJob.case_id == case.id,
                        IssueStatusUpdateJob.entry_sequence <= case.last_entry_sequence,
                        IssueStatusUpdateJob.state.in_(
                            (IssueStatusUpdateState.PENDING, IssueStatusUpdateState.PROCESSING)
                        ),
                    )
                    .order_by(
                        IssueStatusUpdateJob.entry_sequence.asc(),
                        IssueStatusUpdateJob.created_at.asc(),
                    )
                    .with_for_update()
                ).all()
                if not active_jobs:
                    continue
                claims: list[IssueStatusUpdateClaim] = []
                for active_job in active_jobs:
                    token = uuid4()
                    active_job.state = IssueStatusUpdateState.PROCESSING
                    active_job.lock_token = token
                    active_job.locked_until = now + lease
                    claims.append(
                        IssueStatusUpdateClaim(
                            id=active_job.id,
                            lock_token=token,
                            entry_sequence=active_job.entry_sequence,
                        )
                    )
                entries = session.scalars(
                    select(CaseEntry)
                    .where(
                        CaseEntry.case_id == case.id,
                        CaseEntry.sequence <= case.last_entry_sequence,
                    )
                    .order_by(CaseEntry.sequence.asc())
                ).all()
                work.append(
                    IssueStatusUpdateWork(
                        case_id=case.id,
                        id=claims[0].id,
                        claims=tuple(claims),
                        case_ref=case.case_ref,
                        entry_sequence=max(claim.entry_sequence for claim in claims),
                        trigger_sequences=tuple(claim.entry_sequence for claim in claims),
                        title=case.title,
                        issue_description=case.issue_description,
                        suspected_type=case.suspected_type,
                        current_situation=case.issue_current_situation,
                        current_status=case.issue_current_status,
                        workflow_status=case.status,
                        waiting_on=case.waiting_on,
                        timeline=self._status_update_timeline(session, entries),
                    )
                )
            return tuple(work)

    def list_issue_status_queue(self, limit: int = 100) -> tuple[IssueStatusQueueItem, ...]:
        """Return active status-evaluation jobs across all issues for the queue page."""
        page_size = max(1, min(limit, 200))
        with self._session_factory() as session:
            rows = session.execute(
                select(IssueStatusUpdateJob, Case)
                .join(Case, Case.id == IssueStatusUpdateJob.case_id)
                .where(
                    IssueStatusUpdateJob.state.in_(
                        (IssueStatusUpdateState.PENDING, IssueStatusUpdateState.PROCESSING)
                    )
                )
                .order_by(
                    IssueStatusUpdateJob.created_at.asc(),
                    IssueStatusUpdateJob.entry_sequence.asc(),
                )
                .limit(page_size)
            ).all()
            return tuple(
                IssueStatusQueueItem(
                    id=job.id,
                    case_ref=case.case_ref,
                    bug_number=case.bug_number,
                    customer_name=case.customer_name,
                    title=case.title,
                    entry_sequence=job.entry_sequence,
                    state=job.state.value,
                    attempts=job.attempts,
                    created_at=job.created_at,
                    next_attempt_at=job.next_attempt_at,
                    last_error=job.last_error,
                )
                for job, case in rows
            )

    def start_issue_draft_processing(self) -> UUID:
        """Register a workbench draft request while its Agent call is running."""
        now = utc_now()
        task_id = uuid4()
        stale_before = now - timedelta(minutes=30)
        with self._session_factory() as session, session.begin():
            session.execute(
                delete(IssueDraftProcessingTask).where(
                    IssueDraftProcessingTask.started_at < stale_before
                )
            )
            session.add(IssueDraftProcessingTask(id=task_id, started_at=now))
        return task_id

    def finish_issue_draft_processing(self, task_id: UUID) -> None:
        """Remove a workbench draft request after it succeeds or fails."""
        with self._session_factory() as session, session.begin():
            session.execute(
                delete(IssueDraftProcessingTask).where(IssueDraftProcessingTask.id == task_id)
            )

    def list_issue_draft_processing_tasks(
        self, limit: int = 100
    ) -> tuple[IssueDraftProcessingQueueItem, ...]:
        """Return recent, currently running workbench draft requests."""
        page_size = max(1, min(limit, 200))
        now = utc_now()
        stale_before = now - timedelta(minutes=30)
        with self._session_factory() as session, session.begin():
            session.execute(
                delete(IssueDraftProcessingTask).where(
                    IssueDraftProcessingTask.started_at < stale_before
                )
            )
            rows = session.scalars(
                select(IssueDraftProcessingTask)
                .where(IssueDraftProcessingTask.started_at >= stale_before)
                .order_by(IssueDraftProcessingTask.started_at.asc())
                .limit(page_size)
            ).all()
            return tuple(
                IssueDraftProcessingQueueItem(
                    id=row.id,
                    started_at=_as_utc(row.started_at),
                )
                for row in rows
            )

    def list_recent_issue_status_changes(
        self, limit: int = 50
    ) -> tuple[RecentIssueStatusChange, ...]:
        """Return recent workflow and issue-conclusion status transitions."""
        page_size = max(1, min(limit, 100))
        batch_size = max(page_size * 4, 200)
        with self._session_factory() as session:
            results: list[RecentIssueStatusChange] = []
            cursor_created_at: datetime | None = None
            cursor_entry_id: UUID | None = None
            while len(results) < page_size:
                statement = (
                    select(CaseEntry, Case)
                    .join(Case, Case.id == CaseEntry.case_id)
                    .where(
                        CaseEntry.kind.in_(
                            (CaseEntryKind.STATUS_CHANGED, CaseEntryKind.CASE_METADATA_UPDATED)
                        )
                    )
                    .order_by(CaseEntry.created_at.desc(), CaseEntry.id.desc())
                )
                if cursor_created_at is not None and cursor_entry_id is not None:
                    statement = statement.where(
                        or_(
                            CaseEntry.created_at < cursor_created_at,
                            and_(
                                CaseEntry.created_at == cursor_created_at,
                                CaseEntry.id < cursor_entry_id,
                            ),
                        )
                    )
                rows = session.execute(statement.limit(batch_size)).all()
                if not rows:
                    break
                for entry, case in rows:
                    metadata = entry.metadata_json or {}
                    if entry.kind is CaseEntryKind.STATUS_CHANGED:
                        from_status = metadata.get("from_status")
                        to_status = metadata.get("to_status")
                        if not isinstance(from_status, str) or not isinstance(to_status, str):
                            continue
                        results.append(
                            RecentIssueStatusChange(
                                case_ref=case.case_ref,
                                bug_number=case.bug_number,
                                customer_name=case.customer_name,
                                title=case.title,
                                field_label="协作流程状态",
                                from_status=from_status,
                                to_status=to_status,
                                actor_name=entry.actor_name_snapshot,
                                created_at=entry.created_at,
                            )
                        )
                    elif metadata.get("action") in {
                        "set_issue_current_status",
                        "agent_issue_status_update",
                    }:
                        changes = metadata.get("changes")
                        status_change = changes.get("issue_current_status") if isinstance(
                            changes, Mapping
                        ) else None
                        if not isinstance(status_change, Mapping):
                            continue
                        from_status = status_change.get("from")
                        to_status = status_change.get("to")
                        if not isinstance(from_status, str) or not isinstance(to_status, str):
                            continue
                        results.append(
                            RecentIssueStatusChange(
                                case_ref=case.case_ref,
                                bug_number=case.bug_number,
                                customer_name=case.customer_name,
                                title=case.title,
                                field_label="问题结论状态",
                                from_status=from_status,
                                to_status=to_status,
                                actor_name=entry.actor_name_snapshot,
                                created_at=entry.created_at,
                            )
                        )
                    if len(results) >= page_size:
                        break
                if len(rows) < batch_size:
                    break
                last_entry = rows[-1][0]
                cursor_created_at = last_entry.created_at
                cursor_entry_id = last_entry.id
            return tuple(results[:page_size])

    def complete_issue_status_update(
        self, claims: Sequence[IssueStatusUpdateClaim], issue_current_status: str
    ) -> int:
        if issue_current_status not in {status.value for status in IssueCurrentStatus}:
            raise ValidationError("Agent 返回了无效的问题状态")
        if not claims:
            return 0
        claim_by_id = {claim.id: claim.lock_token for claim in claims}
        if len(claim_by_id) != len(claims):
            raise ValidationError("状态判定批次中包含重复任务")
        with self._session_factory() as session, session.begin():
            case_id = session.scalar(
                select(IssueStatusUpdateJob.case_id).where(
                    IssueStatusUpdateJob.id == claims[0].id,
                    IssueStatusUpdateJob.state == IssueStatusUpdateState.PROCESSING,
                    IssueStatusUpdateJob.lock_token == claim_by_id[claims[0].id],
                )
            )
            if case_id is None:
                return 0
            case = session.scalar(select(Case).where(Case.id == case_id).with_for_update())
            if case is None:
                return 0
            jobs = session.scalars(
                select(IssueStatusUpdateJob)
                .where(
                    IssueStatusUpdateJob.id.in_(tuple(claim_by_id)),
                    IssueStatusUpdateJob.case_id == case.id,
                    IssueStatusUpdateJob.state == IssueStatusUpdateState.PROCESSING,
                    IssueStatusUpdateJob.lock_token.in_(tuple(claim_by_id.values())),
                )
                .with_for_update()
            ).all()
            jobs_by_id = {job.id: job for job in jobs}
            if len({job.case_id for job in jobs}) != 1:
                return 0
            if len(jobs_by_id) != len(claim_by_id) or any(
                jobs_by_id.get(job_id) is None
                or jobs_by_id[job_id].lock_token != lock_token
                for job_id, lock_token in claim_by_id.items()
            ):
                return 0
            for job in jobs:
                job.state = IssueStatusUpdateState.COMPLETED
                job.completed_at = utc_now()
                job.lock_token = None
                job.locked_until = None
                job.last_error = None
            completed_count = len(jobs)
            if case.issue_current_status == issue_current_status:
                return completed_count

            previous_status = case.issue_current_status
            case.issue_current_status = issue_current_status
            case.version += 1
            self._append_entry(
                session,
                case=case,
                kind=CaseEntryKind.CASE_METADATA_UPDATED,
                side=EntrySide.SYSTEM,
                actor_user=None,
                actor_name="状态 Agent",
                metadata={
                    "action": "agent_issue_status_update",
                    "changes": {
                        "issue_current_status": {
                            "from": previous_status,
                            "to": issue_current_status,
                        }
                    },
                },
            )
            return completed_count

    def fail_issue_status_update(
        self, claims: Sequence[IssueStatusUpdateClaim], error: str
    ) -> int:
        if not claims:
            return 0
        claim_by_id = {claim.id: claim.lock_token for claim in claims}
        if len(claim_by_id) != len(claims):
            raise ValidationError("状态判定批次中包含重复任务")
        now = utc_now()
        with self._session_factory() as session, session.begin():
            jobs = session.scalars(
                select(IssueStatusUpdateJob)
                .where(
                    IssueStatusUpdateJob.id.in_(tuple(claim_by_id)),
                    IssueStatusUpdateJob.state == IssueStatusUpdateState.PROCESSING,
                )
                .with_for_update()
            ).all()
            jobs_by_id = {job.id: job for job in jobs}
            if (
                len({job.case_id for job in jobs}) != 1
                or len(jobs_by_id) != len(claim_by_id)
                or any(
                    jobs_by_id.get(job_id) is None
                    or jobs_by_id[job_id].lock_token != lock_token
                    for job_id, lock_token in claim_by_id.items()
                )
            ):
                return 0
            next_attempts = [job.attempts + 1 for job in jobs]
            retry_after = STATUS_UPDATE_RETRY_DELAYS[
                min(max(next_attempts) - 1, len(STATUS_UPDATE_RETRY_DELAYS) - 1)
            ]
            for job in jobs:
                job.attempts += 1
                job.state = IssueStatusUpdateState.PENDING
                job.next_attempt_at = now + retry_after
                job.lock_token = None
                job.locked_until = None
                job.last_error = error[:2000]
            return len(jobs)

    def complete_deferred_deliveries(self, delivery_ids: Sequence[UUID]) -> int:
        """Finalize group deliveries that were sent through a passive callback."""
        normalized_ids = tuple(dict.fromkeys(delivery_ids))
        if not normalized_ids:
            return 0
        completed = 0
        with self._session_factory() as session, session.begin():
            rows = session.execute(
                select(Delivery, CaseEntry, Case)
                .join(CaseEntry, CaseEntry.id == Delivery.entry_id)
                .join(Case, Case.id == CaseEntry.case_id)
                .where(Delivery.id.in_(normalized_ids))
                .with_for_update()
            ).all()
            for delivery, entry, case in rows:
                items = session.scalars(
                    select(DeliveryItem)
                    .where(DeliveryItem.delivery_id == delivery.id)
                    .with_for_update()
                ).all()
                if not self._is_deferred_pending_delivery(delivery, items):
                    continue
                for item in items:
                    item.platform_result_json = {"delivery_mode": DEFERRED_PASSIVE_COMPLETED_MODE}
                    item.last_error = None
                delivery.last_error = None
                delivery.lock_token = None
                delivery.locked_until = None
                if self._may_apply_delivery_transition(session, case, entry, delivery):
                    assert delivery.waiting_on_after_delivery is not None
                    case.waiting_on = delivery.waiting_on_after_delivery
                    case.version += 1
                completed += 1
        return completed

    def restore_deferred_deliveries(self, delivery_ids: Sequence[UUID]) -> int:
        """Return suppressed group deliveries to the normal active-push worker."""
        normalized_ids = tuple(dict.fromkeys(delivery_ids))
        if not normalized_ids:
            return 0
        restored = 0
        with self._session_factory() as session, session.begin():
            rows = session.scalars(
                select(Delivery).where(Delivery.id.in_(normalized_ids)).with_for_update()
            ).all()
            for delivery in rows:
                items = session.scalars(
                    select(DeliveryItem)
                    .where(DeliveryItem.delivery_id == delivery.id)
                    .with_for_update()
                ).all()
                if not self._is_deferred_pending_delivery(delivery, items):
                    continue
                self._restore_deferred_delivery(delivery, items)
                restored += 1
        return restored

    def get_delivery_markdown_content(self, delivery_id: UUID) -> str | None:
        """Read the persisted Markdown body used by a callback-bound reply."""
        with self._session_factory() as session:
            items = session.scalars(
                select(DeliveryItem)
                .where(DeliveryItem.delivery_id == delivery_id)
                .order_by(DeliveryItem.position.asc())
            ).all()
            for item in items:
                content = item.payload_json.get("content")
                if item.kind is PartKind.TEXT and isinstance(content, str):
                    return content
        return None

    def recover_deferred_deliveries(self) -> int:
        """Restore passive placeholders left by a worker that stopped mid-reply."""
        restored = 0
        with self._session_factory() as session, session.begin():
            deliveries = session.scalars(
                select(Delivery).where(Delivery.status == DeliveryStatus.SENT).with_for_update()
            ).all()
            for delivery in deliveries:
                items = session.scalars(
                    select(DeliveryItem)
                    .where(DeliveryItem.delivery_id == delivery.id)
                    .with_for_update()
                ).all()
                if not self._is_deferred_pending_delivery(delivery, items):
                    continue
                self._restore_deferred_delivery(delivery, items)
                restored += 1
        return restored

    def _create_case(
        self, session: Session, command: CreateCase, actor: Actor | GroupActor
    ) -> CommandResult:
        if isinstance(actor, Actor):
            actor_user = self._directory.get_user(session, actor.user_id)
            self._directory.assert_consultant_in_queue(
                session, consultant_id=actor.user_id, queue_id=command.consult_queue_id
            )
            actor_id = actor.user_id
            handler_name = command.consult_handler_name or actor_user.display_name
        else:
            if actor.side is not EntrySide.CONSULT or not command.origin_chatid:
                raise Forbidden("只有已绑定的咨询群可以创建问题")
            source_team = self._directory.active_team_for_chatid(session, command.origin_chatid)
            if source_team.kind is not TeamKind.CONSULT_QUEUE:
                raise Forbidden("当前群聊不是咨询队列群")
            if source_team.id != command.consult_queue_id:
                raise Forbidden("咨询群与问题所属队列不匹配")
            actor_user = None
            actor_id = None
            handler_name = source_team.name
        if command.source_msgid:
            existing = self._existing_source_entry(session, command.source_msgid)
            if existing is not None:
                existing_case = self._case_by_id(session, existing.case_id)
                if actor_id is not None:
                    self._directory.assert_case_visible(session, actor_id, existing_case)
                elif existing_case.consult_queue_id != command.consult_queue_id:
                    raise Conflict("该群聊消息已归入另一个问题")
                return self._result(existing_case, existing.id, idempotent=True)

        parts = self._validate_parts(command.parts)
        source_msgid = self._clean_source_msgid(command.source_msgid)
        if source_msgid is not None:
            existing = self._existing_source_entry(session, source_msgid)
            if existing is not None:
                existing_case = self._case_by_id(session, existing.case_id)
                if actor_id is not None:
                    self._directory.assert_case_visible(session, actor_id, existing_case)
                elif existing_case.consult_queue_id != command.consult_queue_id:
                    raise Conflict("该群聊消息已归入另一个问题")
                return self._result(existing_case, existing.id, idempotent=True)
        self._assert_media_exists(session, parts)
        initial_acceptance_team_id = command.initial_acceptance_team_id or command.dev_team_id
        dev_team = self._directory.get_team(session, initial_acceptance_team_id, kind=TeamKind.DEV)
        channel = self._directory.active_channel(session, dev_team.id)
        self._directory.active_consult_channel(session, command.consult_queue_id)
        title = self._validated_title(command.title, parts)
        customer_name = self._validated_customer_name(command.customer_name)
        customer_group_name = self._validated_customer_group_name(command.customer_group_name)
        consult_handler_name = self._validated_handler_name(handler_name)
        related_case_id = self._validated_related_case(session, command.related_case_id)
        created_at = utc_now()
        deadline = created_at + timedelta(hours=DEFAULT_CASE_DEADLINE_HOURS)
        if command.deadline is not None:
            if command.deadline.tzinfo is None or command.deadline.utcoffset() is None:
                raise ValidationError("截止时间必须包含时区")
            deadline = command.deadline.astimezone(UTC)
            if deadline <= created_at:
                raise ValidationError("截止时间必须晚于当前时间")
        issue_number = (
            normalize_case_ref(command.issue_number)
            if command.issue_number
            else self._new_unique_case_ref(session)
        )
        if session.scalar(select(Case.id).where(Case.case_ref == issue_number)) is not None:
            raise Conflict("问题编号已经被使用，请重新生成草稿")
        reservation = session.scalar(
            select(IssueIntakeDraft).where(IssueIntakeDraft.issue_number == issue_number)
        )
        if reservation is not None and source_msgid != f"issue-intake:{reservation.id}":
            raise Conflict("问题编号已预留给另一个上传草稿")
        issue_status = (command.issue_current_status or "问题未回复").strip()
        if issue_status not in {status.value for status in IssueCurrentStatus}:
            raise ValidationError("问题状态无效，请从规定选项中选择")
        issue_description = _clean_optional_text(command.issue_description)
        if issue_description and len(issue_description) > 20_000:
            raise ValidationError("问题描述不能超过20000个字符")
        current_situation = _clean_optional_text(command.issue_current_situation)
        if current_situation and len(current_situation) > 20_000:
            raise ValidationError("现状说明不能超过20000个字符")
        suspected_type = _clean_optional_text(command.suspected_type)
        if suspected_type not in {module.value for module in IssueModule}:
            raise ValidationError("请选择有效的问题模块")
        issue_source = _clean_optional_text(command.issue_source)
        if issue_source and len(issue_source) > 256:
            raise ValidationError("问题来源不能超过256个字符")
        bug_number = next_bug_number(session, created_at)
        approaching_window_minutes = (
            command.approaching_window_minutes
            if command.approaching_window_minutes is not None
            else DEFAULT_APPROACHING_WINDOW_MINUTES
        )
        if approaching_window_minutes < MIN_DEADLINE_INCREMENT_MINUTES:
            raise ValidationError("临期窗口不能小于30分钟")
        case = Case(
            id=uuid4(),
            case_ref=issue_number,
            title=title,
            customer_name=customer_name,
            customer_group_name=customer_group_name,
            customer_contact_name=_clean_optional_text(command.customer_contact_name),
            customer_contact_method=_clean_optional_text(command.customer_contact_method),
            priority=command.priority,
            deadline=deadline,
            approaching_window_minutes=approaching_window_minutes,
            lifecycle_status=LifecycleStatus.OPEN,
            status=CaseStatus.PENDING_CONFIRMATION,
            # A handoff cannot move the wait state before its delivery bundle
            # succeeds, so a newly-created event remains on the submitting side.
            waiting_on=WaitingOn.CONSULT,
            consult_queue_id=command.consult_queue_id,
            current_consultant_id=actor_id,
            consult_handler_name=consult_handler_name,
            current_dev_team_id=channel.team_id,
            initial_acceptance_team_id=dev_team.id,
            creator_id=actor_id,
            issue_description=issue_description,
            suspected_type=suspected_type,
            issue_current_situation=current_situation,
            issue_current_status=issue_status,
            bug_number=bug_number,
            issue_source=issue_source,
            issue_metadata_json=dict(command.issue_metadata),
            initial_expires_at=deadline,
            related_case_id=related_case_id,
            version=1,
            last_entry_sequence=0,
            created_at=created_at,
        )
        session.add(case)
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.FORMAL_MESSAGE,
            side=EntrySide.CONSULT,
            actor_user=actor_user,
            actor_name=consult_handler_name,
            intent=MessageIntent.HANDOFF,
            source_msgid=source_msgid,
            metadata={"action": "create_case"},
        )
        self._add_parts(session, entry, parts)
        delivery_id = self._create_formal_delivery(
            session,
            case=case,
            entry=entry,
            destination_type=DeliveryDestination.CHAT,
            destination_address=channel.chatid,
            waiting_on_after_delivery=WaitingOn.DEV,
        )
        return self._result(case, entry.id, delivery_ids=(delivery_id,))

    def _update_case_metadata(
        self, session: Session, command: UpdateCaseMetadata, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_consult_manager(session, actor.user_id, case)
        actor_user = self._directory.get_user(session, actor.user_id)

        new_customer_name = self._validated_customer_name(command.customer_name)
        current_customer_group_name = _case_customer_group_name(case)
        new_customer_group_name = (
            current_customer_group_name
            if command.customer_group_name is None
            else self._validated_customer_group_name(command.customer_group_name)
        )
        new_contact_name = _clean_optional_text(command.customer_contact_name)
        new_contact_method = _clean_optional_text(command.customer_contact_method)
        new_values = {
            "customer_name": new_customer_name,
            "customer_group_name": new_customer_group_name,
            "customer_contact_name": new_contact_name,
            "customer_contact_method": new_contact_method,
            "priority": command.priority.value,
        }
        old_values = {
            "customer_name": case.customer_name,
            "customer_group_name": current_customer_group_name,
            "customer_contact_name": case.customer_contact_name,
            "customer_contact_method": case.customer_contact_method,
            "priority": case.priority.value,
        }
        changes = {
            key: {"from": old_values[key], "to": value}
            for key, value in new_values.items()
            if old_values[key] != value
        }
        if not changes:
            return self._result(case)

        case.customer_name = new_customer_name
        case.customer_group_name = new_customer_group_name
        case.customer_contact_name = new_contact_name
        case.customer_contact_method = new_contact_method
        case.priority = command.priority
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CASE_METADATA_UPDATED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={"changes": changes},
        )
        return self._result(case, entry.id)

    def _set_issue_current_status(
        self, session: Session, command: SetIssueCurrentStatus, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_consult_manager(session, actor.user_id, case)
        if command.issue_current_status not in {status.value for status in IssueCurrentStatus}:
            raise ValidationError("问题状态无效，请从规定选项中选择")
        if case.issue_current_status == command.issue_current_status:
            return self._result(case, None, idempotent=True)

        actor_user = self._directory.get_user(session, actor.user_id)
        previous_status = case.issue_current_status
        case.issue_current_status = command.issue_current_status
        case.version += 1
        pending_jobs = session.scalars(
            select(IssueStatusUpdateJob)
            .where(
                IssueStatusUpdateJob.case_id == case.id,
                IssueStatusUpdateJob.state.in_(
                    (IssueStatusUpdateState.PENDING, IssueStatusUpdateState.PROCESSING)
                ),
            )
            .with_for_update()
        ).all()
        for job in pending_jobs:
            job.state = IssueStatusUpdateState.CANCELLED
            job.lock_token = None
            job.locked_until = None
            job.last_error = None
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CASE_METADATA_UPDATED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "action": "set_issue_current_status",
                "changes": {
                    "issue_current_status": {
                        "from": previous_status,
                        "to": command.issue_current_status,
                    }
                },
            },
        )
        return self._result(case, entry.id)

    def _adjust_case_deadline(
        self,
        session: Session,
        command: AdjustCaseDeadline | ExtendCaseDeadline,
        actor: Actor,
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        if case.lifecycle_status is not LifecycleStatus.OPEN:
            raise ValidationError("已关闭问题不能调整期限")
        self._assert_consult_deadline_access(session, actor.user_id, case)
        if isinstance(command, AdjustCaseDeadline):
            adjustment_minutes = _validated_deadline_adjustment_minutes(command.adjustment_minutes)
            action = "adjust_deadline"
        else:
            adjustment_minutes = _validated_deadline_minutes(command.extension_minutes, "延长期限")
            action = "extend_deadline"
        actor_user = self._directory.get_user(session, actor.user_id)

        old_deadline = _as_utc(case.deadline)
        try:
            new_deadline = old_deadline + timedelta(minutes=adjustment_minutes)
        except OverflowError as error:
            raise ValidationError("截止时间调整超出可设置范围") from error
        case.deadline = new_deadline
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CASE_METADATA_UPDATED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "action": action,
                "changes": {
                    "deadline": {
                        "from": old_deadline.isoformat(),
                        "to": new_deadline.isoformat(),
                    }
                },
            },
        )
        return self._result(case, entry.id)

    def _set_case_deadline(
        self, session: Session, command: SetCaseDeadline, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        if case.lifecycle_status is not LifecycleStatus.OPEN:
            raise ValidationError("已关闭问题不能调整期限")
        self._assert_consult_deadline_access(session, actor.user_id, case)
        if command.deadline.tzinfo is None or command.deadline.utcoffset() is None:
            raise ValidationError("截止时间必须包含时区")

        old_deadline = _as_utc(case.deadline)
        new_deadline = _as_utc(command.deadline)
        if old_deadline == new_deadline:
            return self._result(case)

        actor_user = self._directory.get_user(session, actor.user_id)
        case.deadline = new_deadline
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CASE_METADATA_UPDATED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "action": "set_deadline",
                "changes": {
                    "deadline": {
                        "from": old_deadline.isoformat(),
                        "to": new_deadline.isoformat(),
                    }
                },
            },
        )
        return self._result(case, entry.id)

    def _set_case_approaching_window(
        self, session: Session, command: SetCaseApproachingWindow, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        if case.lifecycle_status is not LifecycleStatus.OPEN:
            raise ValidationError("已关闭问题不能调整期限")
        self._assert_consult_deadline_access(session, actor.user_id, case)
        approaching_minutes = _validated_deadline_minutes(
            command.approaching_window_minutes, "临近期限提醒时间"
        )
        if approaching_minutes == case.approaching_window_minutes:
            return self._result(case)
        actor_user = self._directory.get_user(session, actor.user_id)
        old_minutes = case.approaching_window_minutes
        case.approaching_window_minutes = approaching_minutes
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CASE_METADATA_UPDATED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "action": "set_approaching_window",
                "changes": {
                    "approaching_window_minutes": {
                        "from": old_minutes,
                        "to": approaching_minutes,
                    }
                },
            },
        )
        return self._result(case, entry.id)

    def _post_formal_message(
        self,
        session: Session,
        command: PostFormalMessage,
        actor: Actor | GroupActor,
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        actor_user: User | None
        actor_name: str | None = None
        if isinstance(actor, GroupActor):
            if command.side is not actor.side or not command.origin_chatid:
                raise Forbidden("群聊身份与消息侧别不匹配")
            origin_kind = self._directory.team_kind_for_chatid(session, command.origin_chatid)
            if actor.side is EntrySide.CONSULT:
                expected_kind = TeamKind.CONSULT_QUEUE
            elif actor.side is EntrySide.DEV:
                expected_kind = TeamKind.DEV
            else:
                expected_kind = None
            if expected_kind is None or origin_kind is not expected_kind:
                raise Forbidden("当前群聊未绑定到对应的问题沟通侧")
            actor_user = None
            side = actor.side
            if side is EntrySide.CONSULT:
                actor_name = case.consult_handler_name
            else:
                dev_team = self._directory.get_team(
                    session, case.current_dev_team_id, kind=TeamKind.DEV
                )
                actor_name = dev_team.lead_display_name or dev_team.name
        else:
            actor_user = self._directory.get_user(session, actor.user_id)
            side = cast(
                EntrySide,
                self._directory.side_for_actor(session, actor.user_id, case, command.side),
            )
        existing = self._existing_source_entry(session, command.source_msgid)
        if existing is not None:
            if existing.case_id != case.id:
                raise Conflict("该企业微信消息已归入另一个问题")
            return self._result(case, existing.id, idempotent=True)
        self._assert_expected_version(case, command.expected_version)
        self._assert_open(case)
        parts = self._validate_parts(command.parts)
        self._assert_media_exists(session, parts)
        previous_status = case.status
        mark_processing = (
            isinstance(actor, GroupActor)
            and side is EntrySide.DEV
            and case.lifecycle_status is LifecycleStatus.OPEN
            and case.status
            in {
                CaseStatus.PENDING_CONFIRMATION,
                CaseStatus.WAITING_CUSTOMER,
                CaseStatus.SUSPENDED,
            }
        )
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.FORMAL_MESSAGE,
            side=side,
            actor_user=actor_user,
            actor_name=actor_name,
            intent=command.intent,
            source_msgid=self._clean_source_msgid(command.source_msgid),
            metadata={"action": "post_formal_message"},
        )
        self._add_parts(session, entry, parts)
        session.add(
            IssueStatusUpdateJob(
                id=uuid4(),
                case_id=case.id,
                entry_id=entry.id,
                entry_sequence=entry.sequence,
                next_attempt_at=utc_now(),
            )
        )
        destination_type, destination_address = self._destination_for_side(session, case, side)
        wait_after = (
            self._opposite_waiting_side(side) if command.intent is MessageIntent.HANDOFF else None
        )
        delivery_status = (
            DeliveryStatus.SENT if command.suppress_delivery else DeliveryStatus.PENDING
        )
        item_status = (
            DeliveryItemStatus.SENT if command.suppress_delivery else DeliveryItemStatus.PENDING
        )
        item_platform_result = (
            {"delivery_mode": DEFERRED_PASSIVE_PENDING_MODE} if command.suppress_delivery else None
        )
        delivery_ids = [
            self._create_formal_delivery(
                session,
                case=case,
                entry=entry,
                destination_type=destination_type,
                destination_address=destination_address,
                waiting_on_after_delivery=wait_after,
                delivery_status=delivery_status,
                item_status=item_status,
                item_platform_result=item_platform_result,
            )
        ]
        if mark_processing:
            case.status = CaseStatus.IN_PROGRESS
            self._append_entry(
                session,
                case=case,
                kind=CaseEntryKind.STATUS_CHANGED,
                side=EntrySide.SYSTEM,
                actor_user=actor_user,
                actor_name=actor_name,
                metadata={
                    "from_status": previous_status.value,
                    "to_status": CaseStatus.IN_PROGRESS.value,
                },
            )
        case.version += 1
        return self._result(case, entry.id, delivery_ids=tuple(delivery_ids))

    def _transfer_consultant(
        self, session: Session, command: TransferConsultant, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_case_transfer_authorized(session, actor.user_id, case)
        actor_user = self._directory.get_user(session, actor.user_id)
        new_consultant = self._directory.assert_consultant_in_queue(
            session, consultant_id=command.new_consultant_id, queue_id=case.consult_queue_id
        )
        if case.current_consultant_id == new_consultant.id:
            raise ValidationError("该人员已经是当前咨询经办人")
        old_consultant = self._user_name(session, case.current_consultant_id)
        case.current_consultant_id = new_consultant.id
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.TRANSFER_CONSULTANT,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "from_consultant": old_consultant,
                "to_consultant": new_consultant.display_name,
            },
        )
        consult_channel = self._directory.active_consult_channel(session, case.consult_queue_id)
        delivery_id = self._create_notice_delivery(
            session,
            entry=entry,
            case=case,
            destination_type=DeliveryDestination.CHAT,
            destination_address=consult_channel.chatid,
            content=(
                f"咨询经办已由{old_consultant or '未指定'}转交给"
                f"{new_consultant.display_name}。"
                f"请后续由{new_consultant.display_name}继续跟进。"
            ),
        )
        return self._result(case, entry.id, delivery_ids=(delivery_id,))

    def _transfer_dev_team(
        self, session: Session, command: TransferDevTeam, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_dev_team_transfer_authorized(session, actor.user_id, case)
        actor_user = self._directory.get_user(session, actor.user_id)
        if case.current_dev_team_id == command.new_dev_team_id:
            raise ValidationError("目标研发责任团队与当前团队相同")
        old_channel = self._directory.active_channel(session, case.current_dev_team_id)
        new_channel = self._directory.active_channel(session, command.new_dev_team_id)
        old_team_id = case.current_dev_team_id
        old_team = session.get(Team, old_team_id)
        new_team = session.get(Team, new_channel.team_id)
        case.current_dev_team_id = new_channel.team_id
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.TRANSFER_DEV_TEAM,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "from_dev_team_id": str(old_team_id),
                "to_dev_team_id": str(new_channel.team_id),
                "from_dev_team": old_team.name if old_team else "未知团队",
                "to_dev_team": new_team.name if new_team else "未知团队",
            },
        )
        old_delivery = self._create_notice_delivery(
            session,
            entry=entry,
            case=case,
            destination_type=DeliveryDestination.CHAT,
            destination_address=old_channel.chatid,
            content="本问题已转出至另一研发责任团队，后续更新不再发送到本群。",
        )
        new_delivery = self._create_notice_delivery(
            session,
            entry=entry,
            case=case,
            destination_type=DeliveryDestination.CHAT,
            destination_address=new_channel.chatid,
            content="本问题已转入本团队，请在问题中心查看完整时间线。",
        )
        return self._result(case, entry.id, delivery_ids=(old_delivery, new_delivery))

    def _close_case(self, session: Session, command: CloseCase, actor: Actor) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_consult_manager(session, actor.user_id, case)
        self._assert_open(case)
        actor_user = self._directory.get_user(session, actor.user_id)
        case.lifecycle_status = LifecycleStatus.CLOSED
        case.status = CaseStatus.CLOSED
        case.waiting_on = WaitingOn.NONE
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CLOSED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={"action": "close_case"},
        )
        return self._result(case, entry.id)

    def _set_case_status(
        self, session: Session, command: SetCaseStatus, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_case_visible(session, actor.user_id, case)
        self._assert_open(case)
        if command.status is CaseStatus.CLOSED:
            raise ValidationError("请通过‘确认客户侧闭环并关闭’操作关闭问题")
        if command.status is CaseStatus.PENDING_CONFIRMATION:
            raise ValidationError("待确定是初始状态，不能通过操作切回")
        if command.status is CaseStatus.WAITING_CUSTOMER:
            self._directory.assert_consult_manager(session, actor.user_id, case)
        if case.status is command.status:
            return self._result(case, None, idempotent=True)

        if command.status is CaseStatus.IN_PROGRESS:
            if case.status not in (
                CaseStatus.PENDING_CONFIRMATION,
                CaseStatus.WAITING_CUSTOMER,
                CaseStatus.SUSPENDED,
            ):
                raise ValidationError("只有待确定、待客户反馈或挂起状态可以受理")
            if case.status is CaseStatus.PENDING_CONFIRMATION and not self._directory.is_member(
                session, user_id=actor.user_id, team_id=case.current_dev_team_id
            ):
                raise Forbidden("待确定状态只能由研发团队成员受理")
        elif command.status is CaseStatus.WAITING_CUSTOMER:
            if case.status is not CaseStatus.IN_PROGRESS:
                raise ValidationError("只有处理中问题可以设为待客户反馈")
        elif command.status is CaseStatus.SUSPENDED:
            if case.status is not CaseStatus.IN_PROGRESS:
                raise ValidationError("只有处理中问题可以挂起")

        actor_user = self._directory.get_user(session, actor.user_id)
        previous_status = case.status
        case.status = command.status
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.STATUS_CHANGED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "from_status": previous_status.value,
                "to_status": command.status.value,
            },
        )
        return self._result(case, entry.id)

    def _reopen_case(self, session: Session, command: ReopenCase, actor: Actor) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_consult_manager(session, actor.user_id, case)
        if case.lifecycle_status is not LifecycleStatus.CLOSED:
            raise ValidationError("只有已关闭问题可以重新打开")
        if command.waiting_on is WaitingOn.NONE:
            raise ValidationError("重新打开问题时必须明确下一步责任方")
        actor_user = self._directory.get_user(session, actor.user_id)
        channel: TeamChannel | None = None
        if command.waiting_on is WaitingOn.DEV:
            channel = self._directory.active_channel(session, case.current_dev_team_id)
        case.lifecycle_status = LifecycleStatus.OPEN
        case.status = CaseStatus.IN_PROGRESS
        case.waiting_on = command.waiting_on
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.REOPENED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={"waiting_on": command.waiting_on.value},
        )
        delivery_ids: tuple[UUID, ...] = ()
        if channel is not None:
            delivery_id = self._create_notice_delivery(
                session,
                entry=entry,
                case=case,
                destination_type=DeliveryDestination.CHAT,
                destination_address=channel.chatid,
                content="咨询侧已重新打开本问题，请研发继续处理。",
            )
            delivery_ids = (delivery_id,)
        return self._result(case, entry.id, delivery_ids=delivery_ids)

    def _correct_entry(
        self, session: Session, command: CorrectEntry, actor: Actor
    ) -> CommandResult:
        if normalize_case_ref(command.case_ref) == normalize_case_ref(command.target_case_ref):
            raise ValidationError("纠错目标必须是另一个问题")
        cases = self._load_cases_for_update(session, command.case_ref, command.target_case_ref)
        source = cases[normalize_case_ref(command.case_ref)]
        target = cases[normalize_case_ref(command.target_case_ref)]
        self._assert_expected_version(source, command.expected_version)
        self._assert_expected_version(target, command.target_expected_version)
        self._directory.assert_consult_manager(session, actor.user_id, source)
        self._directory.assert_case_visible(session, actor.user_id, target)
        self._assert_open(target)
        actor_user = self._directory.get_user(session, actor.user_id)
        original = session.scalar(
            select(CaseEntry)
            .where(CaseEntry.id == command.entry_id, CaseEntry.case_id == source.id)
            .with_for_update()
        )
        if original is None:
            raise NotFound("待纠正的时间线记录不存在")
        if original.kind is not CaseEntryKind.FORMAL_MESSAGE:
            raise ValidationError("只能纠正正式消息")
        original_parts = self._entry_parts(session, original.id)
        if not original_parts:
            raise ValidationError("该正式消息没有可纠正的内容")
        copied_parts = tuple(self._part_from_orm(part) for part in original_parts)
        source.version += 1
        target.version += 1
        source_entry = self._append_entry(
            session,
            case=source,
            kind=CaseEntryKind.CORRECTION,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            corrects_entry_id=original.id,
            metadata={"corrected_to_case_ref": target.case_ref, "action": "mark_misrouted"},
        )
        corrected_entry = self._append_entry(
            session,
            case=target,
            kind=CaseEntryKind.CORRECTION,
            side=original.side,
            actor_user=actor_user,
            intent=original.message_intent,
            corrects_entry_id=original.id,
            metadata={
                "corrected_from_case_ref": source.case_ref,
                "action": "copy_misrouted_message",
                "original_actor_name": original.actor_name_snapshot,
            },
        )
        self._add_parts(session, corrected_entry, copied_parts)
        source_destination_type, source_destination_address = self._destination_for_side(
            session, source, original.side
        )
        source_delivery = self._create_notice_delivery(
            session,
            entry=source_entry,
            case=source,
            destination_type=source_destination_type,
            destination_address=source_destination_address,
            content="上一条正式消息已被标记为误发；请以问题中心中的纠错记录为准。",
        )
        target_destination_type, target_destination_address = self._destination_for_side(
            session, target, original.side
        )
        wait_after = (
            self._opposite_waiting_side(original.side)
            if original.message_intent is MessageIntent.HANDOFF
            else None
        )
        corrected_delivery = self._create_formal_delivery(
            session,
            case=target,
            entry=corrected_entry,
            destination_type=target_destination_type,
            destination_address=target_destination_address,
            waiting_on_after_delivery=wait_after,
        )
        return self._result(
            source,
            source_entry.id,
            delivery_ids=(source_delivery, corrected_delivery),
        )

    def _delivery_item_succeeded(
        self, session: Session, command: DeliveryItemSucceeded
    ) -> CommandResult:
        delivery, entry, case = self._locked_delivery_context(session, command.delivery_id)
        self._assert_delivery_lease(delivery, command.lock_token)
        item = self._locked_delivery_item(session, delivery.id, command.item_id)
        if item.status is DeliveryItemStatus.SENT:
            return self._result(case, entry.id, idempotent=True)
        if item.status is DeliveryItemStatus.FAILED or delivery.status is DeliveryStatus.FAILED:
            raise Conflict("投递束已经最终失败，不能再写入成功结果")
        item.status = DeliveryItemStatus.SENT
        item.last_error = None
        item.platform_result_json = dict(command.platform_result)
        remaining = session.scalar(
            select(DeliveryItem.id).where(
                DeliveryItem.delivery_id == delivery.id,
                DeliveryItem.status != DeliveryItemStatus.SENT,
            )
        )
        if remaining is None:
            delivery.status = DeliveryStatus.SENT
            delivery.last_error = None
            delivery.lock_token = None
            delivery.locked_until = None
            if self._may_apply_delivery_transition(session, case, entry, delivery):
                assert delivery.waiting_on_after_delivery is not None
                case.waiting_on = delivery.waiting_on_after_delivery
                case.version += 1
        return self._result(case, entry.id)

    def _delivery_item_failed(self, session: Session, command: DeliveryItemFailed) -> CommandResult:
        delivery, entry, case = self._locked_delivery_context(session, command.delivery_id)
        self._assert_delivery_lease(delivery, command.lock_token)
        item = self._locked_delivery_item(session, delivery.id, command.item_id)
        if item.status is DeliveryItemStatus.SENT:
            return self._result(case, entry.id, idempotent=True)
        if delivery.status is DeliveryStatus.FAILED:
            return self._result(case, entry.id, idempotent=True)
        safe_error = command.error.strip()[:2000] or "未知投递错误"
        delivery.attempts += 1
        delivery.last_error = safe_error
        item.last_error = safe_error
        delivery.lock_token = None
        delivery.locked_until = None
        if delivery.attempts >= len(RETRY_DELAYS):
            delivery.status = DeliveryStatus.FAILED
            item.status = DeliveryItemStatus.FAILED
        else:
            delivery.next_attempt_at = utc_now() + RETRY_DELAYS[delivery.attempts - 1]
        return self._result(case, entry.id)

    def _validate_parts(self, parts: Sequence[ContentPart]) -> tuple[ContentPart, ...]:
        if not parts:
            raise ValidationError("正式消息至少需要一个文字或图片片段")
        normalized: list[ContentPart] = []
        has_nonempty_text = False
        for part in parts:
            if isinstance(part, TextPart):
                text = part.text.strip()
                if not text:
                    raise ValidationError("文字片段不能为空")
                if len(text) > 20_000:
                    raise ValidationError("单个文字片段不能超过 20000 个字符")
                normalized.append(TextPart(text=text))
                has_nonempty_text = True
            elif isinstance(part, ImagePart):
                normalized.append(part)
            else:
                raise ValidationError("只支持文字和图片片段")
        # Standalone image is deliberately outside the MVP.  An image-first
        # mixed message remains valid as long as it includes a text fragment.
        if not has_nonempty_text:
            raise ValidationError("MVP 的正式消息必须至少包含一个文字片段")
        return tuple(normalized)

    def _assert_media_exists(self, session: Session, parts: Sequence[ContentPart]) -> None:
        media_ids = {part.media_id for part in parts if isinstance(part, ImagePart)}
        if not media_ids:
            return
        existing_ids = set(
            session.scalars(select(StoredMedia.id).where(StoredMedia.id.in_(media_ids))).all()
        )
        if existing_ids != media_ids:
            raise ValidationError("消息引用了不存在的已转存图片")

    def _validated_title(self, title: str, parts: Sequence[ContentPart]) -> str:
        normalized = title.strip()
        if not normalized:
            first_text = next((part.text for part in parts if isinstance(part, TextPart)), "")
            normalized = first_text[:120]
        if not normalized:
            raise ValidationError("问题标题不能为空")
        if len(normalized) > 512:
            raise ValidationError("问题标题不能超过 512 个字符")
        return normalized

    def _validated_customer_name(self, name: str | None) -> str:
        return (name or "").strip() or "未录入"

    def _validated_customer_group_name(self, name: str | None) -> str | None:
        if name is None:
            return None
        if not isinstance(name, str):
            raise ValidationError("客户群聊名称必须是文本")
        normalized = name.strip()
        if len(normalized) > 256:
            raise ValidationError("客户群聊名称不能超过 256 个字符")
        return normalized or None

    def _validated_handler_name(self, name: str) -> str:
        if not isinstance(name, str):
            raise ValidationError("咨询经办人必须是文本")
        normalized = name.strip()
        if not normalized:
            raise ValidationError("咨询经办人不能为空")
        if len(normalized) > 256:
            raise ValidationError("咨询经办人不能超过 256 个字符")
        return normalized

    def _validated_related_case(
        self, session: Session, related_case_id: UUID | None
    ) -> UUID | None:
        if related_case_id is None:
            return None
        if session.get(Case, related_case_id) is None:
            raise NotFound("关联问题不存在")
        return related_case_id

    def _append_entry(
        self,
        session: Session,
        *,
        case: Case,
        kind: CaseEntryKind,
        side: EntrySide,
        actor_user: User | None,
        actor_name: str | None = None,
        intent: MessageIntent | None = None,
        source_msgid: str | None = None,
        corrects_entry_id: UUID | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> CaseEntry:
        if actor_user is None and not actor_name:
            raise ValidationError("问题记录缺少操作人名称")
        case.last_entry_sequence += 1
        entry = CaseEntry(
            id=uuid4(),
            case_id=case.id,
            sequence=case.last_entry_sequence,
            kind=kind,
            side=side,
            actor_user_id=actor_user.id if actor_user is not None else None,
            actor_name_snapshot=actor_name or actor_user.display_name,
            message_intent=intent,
            source_msgid=source_msgid,
            corrects_entry_id=corrects_entry_id,
            metadata_json=dict(metadata or {}),
        )
        session.add(entry)
        return entry

    def _add_parts(self, session: Session, entry: CaseEntry, parts: Sequence[ContentPart]) -> None:
        for position, part in enumerate(parts):
            if isinstance(part, TextPart):
                record = CaseEntryPart(
                    id=uuid4(),
                    entry_id=entry.id,
                    position=position,
                    kind=PartKind.TEXT,
                    text=part.text,
                    media_id=None,
                )
            else:
                record = CaseEntryPart(
                    id=uuid4(),
                    entry_id=entry.id,
                    position=position,
                    kind=PartKind.IMAGE,
                    text=None,
                    media_id=part.media_id,
                )
            session.add(record)

    def _create_formal_delivery(
        self,
        session: Session,
        *,
        case: Case,
        entry: CaseEntry,
        destination_type: DeliveryDestination,
        destination_address: str,
        waiting_on_after_delivery: WaitingOn | None,
        delivery_status: DeliveryStatus = DeliveryStatus.PENDING,
        item_status: DeliveryItemStatus = DeliveryItemStatus.PENDING,
        item_platform_result: Mapping[str, object] | None = None,
    ) -> UUID:
        entry_parts = tuple(
            SourcePart(kind=part.kind, text=part.text, media_id=part.media_id)
            for part in self._entry_parts(session, entry.id)
        )
        try:
            rendered = build_formal_bundle(
                case_ref=case.case_ref,
                bug_number=self._ensure_bug_number(session, case),
                case_title=case.title,
                customer_name=case.customer_name,
                speaker_name=_original_speaker_name(entry),
                parts=entry_parts,
            )
        except ValueError as error:
            raise ValidationError("去除 @ 提及后，正式消息必须保留文字内容") from error
        return self._create_delivery(
            session,
            entry_id=entry.id,
            destination_type=destination_type,
            destination_address=destination_address,
            waiting_on_after_delivery=waiting_on_after_delivery,
            rendered_items=rendered,
            delivery_status=delivery_status,
            item_status=item_status,
            item_platform_result=item_platform_result,
        )

    def _create_notice_delivery(
        self,
        session: Session,
        *,
        entry: CaseEntry,
        case: Case,
        destination_type: DeliveryDestination,
        destination_address: str,
        content: str,
    ) -> UUID:
        return self._create_delivery(
            session,
            entry_id=entry.id,
            destination_type=destination_type,
            destination_address=destination_address,
            waiting_on_after_delivery=None,
            rendered_items=build_notice_bundle(
                case_ref=case.case_ref,
                bug_number=self._ensure_bug_number(session, case),
                case_title=case.title,
                operator_name=entry.actor_name_snapshot,
                content=content,
                history_url=f"{self._web_base_url}/events/{case.case_ref}",
            ),
        )

    def _ensure_bug_number(self, session: Session, case: Case) -> str:
        """Assign a number to legacy cases before rendering a human-facing message."""
        if not case.bug_number:
            case.bug_number = next_bug_number(session, _as_utc(case.created_at))
        assert case.bug_number is not None
        return case.bug_number

    def _create_delivery(
        self,
        session: Session,
        *,
        entry_id: UUID,
        destination_type: DeliveryDestination,
        destination_address: str,
        waiting_on_after_delivery: WaitingOn | None,
        rendered_items: Sequence[object],
        delivery_status: DeliveryStatus = DeliveryStatus.PENDING,
        item_status: DeliveryItemStatus = DeliveryItemStatus.PENDING,
        item_platform_result: Mapping[str, object] | None = None,
    ) -> UUID:
        if not destination_address.strip():
            raise RoutingUnavailable("投递目标会话不存在")
        delivery = Delivery(
            id=uuid4(),
            entry_id=entry_id,
            destination_type=destination_type,
            destination_address=destination_address,
            status=delivery_status,
            waiting_on_after_delivery=waiting_on_after_delivery,
            attempts=0,
            next_attempt_at=utc_now(),
        )
        session.add(delivery)
        for position, rendered in enumerate(rendered_items):
            kind = rendered.kind
            payload = rendered.payload
            item = DeliveryItem(
                id=uuid4(),
                delivery_id=delivery.id,
                position=position,
                kind=kind,
                req_id=f"kefu-{uuid4()}",
                status=item_status,
                payload_json=dict(payload),
                platform_result_json=(
                    dict(item_platform_result) if item_platform_result is not None else None
                ),
            )
            session.add(item)
        return delivery.id

    def _destination_for_side(
        self, session: Session, case: Case, side: EntrySide
    ) -> tuple[DeliveryDestination, str]:
        if side is EntrySide.CONSULT:
            channel = self._directory.active_channel(session, case.current_dev_team_id)
            return DeliveryDestination.CHAT, channel.chatid
        if side is EntrySide.DEV:
            channel = self._directory.active_consult_channel(session, case.consult_queue_id)
            return DeliveryDestination.CHAT, channel.chatid
        raise ValidationError("系统记录没有正式消息投递目标")

    def _opposite_waiting_side(self, side: EntrySide) -> WaitingOn:
        if side is EntrySide.CONSULT:
            return WaitingOn.DEV
        if side is EntrySide.DEV:
            return WaitingOn.CONSULT
        raise ValidationError("系统记录不能改变当前等待方")

    def _load_case(self, session: Session, case_ref: str, *, lock: bool = False) -> Case:
        normalized = normalize_case_ref(case_ref)
        statement = select(Case).where(Case.case_ref == normalized)
        if lock:
            statement = statement.with_for_update()
        case = session.scalar(statement)
        if case is None:
            raise NotFound("问题不存在")
        return case

    def _load_cases_for_update(self, session: Session, *case_refs: str) -> dict[str, Case]:
        normalized_refs = tuple(normalize_case_ref(case_ref) for case_ref in case_refs)
        cases = session.scalars(
            select(Case)
            .where(Case.case_ref.in_(normalized_refs))
            .order_by(Case.case_ref.asc())
            .with_for_update()
        ).all()
        by_ref = {case.case_ref: case for case in cases}
        if set(by_ref) != set(normalized_refs):
            raise NotFound("一个或多个问题不存在")
        return by_ref

    def _case_by_id(self, session: Session, case_id: UUID) -> Case:
        case = session.get(Case, case_id)
        if case is None:
            raise NotFound("问题不存在")
        return case

    def _existing_source_entry(
        self, session: Session, source_msgid: str | None
    ) -> CaseEntry | None:
        cleaned = self._clean_source_msgid(source_msgid)
        if cleaned is None:
            return None
        return session.scalar(select(CaseEntry).where(CaseEntry.source_msgid == cleaned))

    def _new_unique_case_ref(self, session: Session) -> str:
        for _ in range(16):
            candidate = new_case_ref()
            if (
                session.scalar(select(Case.id).where(Case.case_ref == candidate)) is None
                and session.scalar(
                    select(IssueIntakeDraft.id).where(
                        IssueIntakeDraft.issue_number == candidate
                    )
                )
                is None
            ):
                return candidate
        raise Conflict("无法生成未冲突的问题引用标记")

    def _assert_expected_version(self, case: Case, expected_version: int | None) -> None:
        if expected_version is None:
            return
        if expected_version < 1:
            raise ValidationError("问题版本号无效")
        if case.version != expected_version:
            raise Conflict("问题已被其他人更新，请刷新后重新确认")

    def _has_consult_deadline_access(self, session: Session, actor_id: UUID, case: Case) -> bool:
        return self._directory.is_global_admin(session, actor_id) or self._directory.is_member(
            session, user_id=actor_id, team_id=case.consult_queue_id
        )

    def _assert_consult_deadline_access(self, session: Session, actor_id: UUID, case: Case) -> None:
        if not self._has_consult_deadline_access(session, actor_id, case):
            raise Forbidden("只有咨询队列成员可以调整问题期限")

    def _assert_open(self, case: Case) -> None:
        if case.lifecycle_status is not LifecycleStatus.OPEN:
            raise ValidationError("已关闭问题不能继续发送正式消息")

    def _clean_source_msgid(self, source_msgid: str | None) -> str | None:
        if source_msgid is None:
            return None
        cleaned = source_msgid.strip()
        if not cleaned:
            return None
        if len(cleaned) > 256:
            raise ValidationError("消息标识过长")
        return cleaned

    def _entry_parts(self, session: Session, entry_id: UUID) -> list[CaseEntryPart]:
        return session.scalars(
            select(CaseEntryPart)
            .where(CaseEntryPart.entry_id == entry_id)
            .order_by(CaseEntryPart.position.asc())
        ).all()

    def _status_update_timeline(
        self, session: Session, entries: Sequence[CaseEntry]
    ) -> str:
        lines: list[str] = []
        for entry in entries[-50:]:
            text_parts: list[str] = []
            has_image = False
            for part in self._entry_parts(session, entry.id):
                if part.kind is PartKind.TEXT and part.text:
                    text_parts.append(part.text)
                elif part.kind is PartKind.IMAGE:
                    has_image = True
            content = "\n".join(text_parts).strip()
            if has_image:
                content = f"{content}\n[含图片附件]".strip()
            if not content:
                metadata = entry.metadata_json or {}
                if entry.kind is CaseEntryKind.STATUS_CHANGED:
                    status_labels = {
                        CaseStatus.PENDING_CONFIRMATION.value: "待确定",
                        CaseStatus.IN_PROGRESS.value: "处理中",
                        CaseStatus.WAITING_CUSTOMER.value: "待客户反馈",
                        CaseStatus.CLOSED.value: "关闭",
                        CaseStatus.SUSPENDED.value: "挂起",
                    }
                    previous = status_labels.get(
                        str(metadata.get("from_status")), str(metadata.get("from_status"))
                    )
                    updated = status_labels.get(
                        str(metadata.get("to_status")), str(metadata.get("to_status"))
                    )
                    content = f"协作流程状态由“{previous}”变更为“{updated}”"
                elif entry.kind is CaseEntryKind.CLOSED:
                    content = "问题已关闭"
                elif entry.kind is CaseEntryKind.REOPENED:
                    content = "问题已重新打开"
                else:
                    changes = metadata.get("changes")
                    content = f"事件 {entry.kind.value}"
                    if changes:
                        content += f"：{changes}"
            lines.append(
                f"[{entry.sequence} | {entry.side.value} | "
                f"{_as_utc(entry.created_at).isoformat()}] {content}"
            )
        timeline = "\n\n".join(lines)
        if len(timeline) > 30_000:
            timeline = "…（较早记录已省略）…\n" + timeline[-29_980:]
        return timeline

    def _part_from_orm(self, part: CaseEntryPart) -> ContentPart:
        if part.kind is PartKind.TEXT:
            assert part.text is not None
            return TextPart(part.text)
        assert part.media_id is not None
        return ImagePart(part.media_id)

    def _user_name(self, session: Session, user_id: UUID | None) -> str | None:
        if user_id is None:
            return None
        user = self._directory.get_user(session, user_id, require_active=False)
        return user.display_name

    def _result(
        self,
        case: Case,
        entry_id: UUID | None,
        *,
        delivery_ids: tuple[UUID, ...] = (),
        idempotent: bool = False,
    ) -> CommandResult:
        return CommandResult(
            case_ref=case.case_ref,
            case_version=case.version,
            entry_id=entry_id,
            delivery_ids=delivery_ids,
            idempotent=idempotent,
        )

    def _locked_delivery_context(
        self, session: Session, delivery_id: UUID
    ) -> tuple[Delivery, CaseEntry, Case]:
        row = session.execute(
            select(Delivery, CaseEntry, Case)
            .join(CaseEntry, CaseEntry.id == Delivery.entry_id)
            .join(Case, Case.id == CaseEntry.case_id)
            .where(Delivery.id == delivery_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise NotFound("投递记录不存在")
        return cast(tuple[Delivery, CaseEntry, Case], row)

    def _locked_delivery_item(
        self, session: Session, delivery_id: UUID, item_id: UUID
    ) -> DeliveryItem:
        item = session.scalar(
            select(DeliveryItem)
            .where(DeliveryItem.id == item_id, DeliveryItem.delivery_id == delivery_id)
            .with_for_update()
        )
        if item is None:
            raise NotFound("投递项不存在")
        return item

    def _assert_delivery_lease(self, delivery: Delivery, lock_token: UUID) -> None:
        if delivery.status is DeliveryStatus.SENT:
            return
        if delivery.lock_token != lock_token:
            raise Conflict("投递租约已失效，不能写入过期结果")

    def _may_apply_delivery_transition(
        self, session: Session, case: Case, entry: CaseEntry, delivery: Delivery
    ) -> bool:
        if (
            delivery.waiting_on_after_delivery is None
            or case.lifecycle_status is not LifecycleStatus.OPEN
        ):
            return False
        later_handoff = session.scalar(
            select(CaseEntry.id).where(
                CaseEntry.case_id == case.id,
                CaseEntry.sequence > entry.sequence,
                CaseEntry.kind.in_((CaseEntryKind.FORMAL_MESSAGE, CaseEntryKind.CORRECTION)),
                CaseEntry.message_intent == MessageIntent.HANDOFF,
            )
        )
        return later_handoff is None

    def _is_deferred_pending_delivery(
        self, delivery: Delivery, items: Sequence[DeliveryItem]
    ) -> bool:
        if delivery.status is not DeliveryStatus.SENT or not items:
            return False
        return all(
            item.status is DeliveryItemStatus.SENT
            and isinstance(item.platform_result_json, Mapping)
            and item.platform_result_json.get("delivery_mode") == DEFERRED_PASSIVE_PENDING_MODE
            for item in items
        )

    def _restore_deferred_delivery(self, delivery: Delivery, items: Sequence[DeliveryItem]) -> None:
        delivery.status = DeliveryStatus.PENDING
        delivery.next_attempt_at = utc_now()
        delivery.last_error = None
        delivery.lock_token = None
        delivery.locked_until = None
        for item in items:
            item.status = DeliveryItemStatus.PENDING
            item.platform_result_json = None
            item.last_error = None

    def _case_view(
        self,
        session: Session,
        case: Case,
        *,
        can_edit_metadata: bool = False,
        can_change_consult_status: bool = False,
        can_accept_pending: bool = False,
        can_extend_deadline: bool = False,
        can_transfer: bool = False,
        transferable_consultants: tuple[UserOption, ...] = (),
        can_transfer_dev_team: bool = False,
        transferable_dev_teams: tuple[TeamOption, ...] = (),
    ) -> CaseView:
        entries = session.scalars(
            select(CaseEntry).where(CaseEntry.case_id == case.id).order_by(CaseEntry.sequence.asc())
        ).all()
        entry_views: list[EntryView] = []
        for entry in entries:
            entry_views.append(
                EntryView(
                    id=entry.id,
                    sequence=entry.sequence,
                    kind=entry.kind,
                    side=entry.side,
                    actor_user_id=entry.actor_user_id,
                    actor_name_snapshot=entry.actor_name_snapshot,
                    message_intent=entry.message_intent,
                    corrects_entry_id=entry.corrects_entry_id,
                    metadata=dict(entry.metadata_json or {}),
                    created_at=_as_utc(entry.created_at),
                    parts=tuple(
                        PartView(
                            position=part.position,
                            kind=part.kind,
                            text=part.text,
                            media_id=part.media_id,
                        )
                        for part in self._entry_parts(session, entry.id)
                    ),
                )
            )
        deliveries = session.scalars(
            select(Delivery)
            .join(CaseEntry, CaseEntry.id == Delivery.entry_id)
            .where(CaseEntry.case_id == case.id)
            .order_by(Delivery.created_at.asc())
        ).all()
        dev_team = session.get(Team, case.current_dev_team_id)
        initial_team = (
            session.get(Team, case.initial_acceptance_team_id)
            if case.initial_acceptance_team_id is not None
            else None
        )
        now = utc_now()
        return CaseView(
            id=case.id,
            case_ref=case.case_ref,
            title=case.title,
            customer_name=case.customer_name,
            customer_group_name=_case_customer_group_name(case),
            customer_contact_name=case.customer_contact_name,
            customer_contact_method=case.customer_contact_method,
            priority=case.priority,
            status=case.status,
            lifecycle_status=case.lifecycle_status,
            waiting_on=case.waiting_on,
            deadline=_as_utc(case.deadline),
            approaching_window_minutes=case.approaching_window_minutes,
            deadline_status=_case_deadline_status(case, now),
            consult_queue_id=case.consult_queue_id,
            consult_queue_name=(
                queue.name if (queue := session.get(Team, case.consult_queue_id)) else "未知队列"
            ),
            current_consultant_id=case.current_consultant_id,
            current_consultant_name=self._user_name(session, case.current_consultant_id),
            consult_handler_name=case.consult_handler_name,
            current_dev_team_id=case.current_dev_team_id,
            current_dev_team_name=dev_team.name if dev_team else "未知团队",
            current_dev_team_lead_display_name=dev_team.lead_display_name if dev_team else None,
            initial_acceptance_team_id=case.initial_acceptance_team_id,
            initial_acceptance_team_name=initial_team.name if initial_team else None,
            issue_description=case.issue_description,
            suspected_type=case.suspected_type,
            current_situation=case.issue_current_situation,
            issue_current_status=case.issue_current_status,
            bug_number=case.bug_number,
            first_recorder_id=case.creator_id,
            first_recorder_name=self._user_name(session, case.creator_id)
            or case.consult_handler_name,
            issue_source=case.issue_source,
            initial_expires_at=(
                _as_utc(case.initial_expires_at) if case.initial_expires_at else None
            ),
            created_at=_as_utc(case.created_at),
            updated_at=_as_utc(case.updated_at),
            version=case.version,
            can_edit_metadata=can_edit_metadata,
            can_change_consult_status=can_change_consult_status,
            can_accept_pending=can_accept_pending,
            can_extend_deadline=can_extend_deadline,
            can_transfer=can_transfer,
            transferable_consultants=transferable_consultants,
            can_transfer_dev_team=can_transfer_dev_team,
            transferable_dev_teams=transferable_dev_teams,
            entries=tuple(entry_views),
            deliveries=tuple(
                DeliveryView(
                    id=delivery.id,
                    entry_id=delivery.entry_id,
                    destination_type=delivery.destination_type,
                    destination_address=delivery.destination_address,
                    status=delivery.status,
                    attempts=delivery.attempts,
                    last_error=delivery.last_error,
                )
                for delivery in deliveries
            ),
        )

    def _case_summary(self, case: Case, developer_team_ids: frozenset[UUID]) -> CaseSummary:
        return CaseSummary(
            case_ref=case.case_ref,
            title=case.title,
            customer_name=case.customer_name,
            bug_number=case.bug_number,
            issue_current_status=case.issue_current_status,
            priority=case.priority,
            status=case.status,
            lifecycle_status=case.lifecycle_status,
            waiting_on=case.waiting_on,
            deadline=_as_utc(case.deadline),
            approaching_window_minutes=case.approaching_window_minutes,
            deadline_status=_case_deadline_status(case),
            current_consultant_id=case.current_consultant_id,
            updated_at=_as_utc(case.updated_at),
            version=case.version,
            can_accept=case.current_dev_team_id in developer_team_ids,
        )
