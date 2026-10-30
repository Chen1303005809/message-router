"""CaseDeskQueries keeps CaseDesk behavior grouped by responsibility."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy import case as sql_case
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import (
    Actor,
    CaseFilter,
    CaseOverview,
    CaseSummary,
    CaseView,
    DeliveryView,
    EntryView,
    IssueDraftProcessingQueueItem,
    IssueStatusQueueItem,
    MediaAccess,
    Page,
    PartView,
    RecentIssueStatusChange,
    TeamOption,
    UserOption,
)
from kefu.case_desk.errors import Forbidden, NotFound, RoutingUnavailable
from kefu.persistence.models import (
    Case,
    CaseEntry,
    CaseEntryKind,
    CaseEntryPart,
    CaseStatus,
    Delivery,
    DeliveryItem,
    IssueDraftProcessingTask,
    IssueStatusUpdateJob,
    IssueStatusUpdateState,
    LifecycleStatus,
    PartKind,
    StoredMedia,
    Team,
    TeamKind,
    WaitingOn,
)

from .service_support import (
    _as_utc,
    _case_customer_group_name,
    _case_deadline_status,
    utc_now,
)


class CaseDeskQueries:
    """CaseDesk Queries operations."""

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
            search_query = case_filter.query.strip()
            if search_query:
                escaped_query = (
                    search_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                search_pattern = f"%{escaped_query}%"
                statement = statement.where(
                    or_(
                        Case.bug_number.ilike(search_pattern, escape="\\"),
                        Case.customer_name.ilike(search_pattern, escape="\\"),
                    )
                )
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
                        status_change = (
                            changes.get("issue_current_status")
                            if isinstance(changes, Mapping)
                            else None
                        )
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

    def _status_update_timeline(self, session: Session, entries: Sequence[CaseEntry]) -> str:
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
