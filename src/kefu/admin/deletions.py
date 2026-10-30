"""Authorized content deletion operations for the admin service."""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import Actor
from kefu.case_desk.errors import Conflict, Forbidden, NotFound
from kefu.media.storage import MediaIngestError
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
    IssueInspectionState,
    IssueIntakeDraft,
    IssueStatusUpdateJob,
    LifecycleStatus,
    StoredMedia,
    Team,
    TeamKind,
    TeamMembership,
    User,
    WeComChannel,
)

from .support import (
    ContentDeletionQueueItem,
    DeletionResult,
    _as_utc,
    _draft_references_team,
    _image_ids,
    _issue_deletion_label,
    _team_kind_label,
    utc_now,
)

logger = logging.getLogger(__name__)


class ContentDeletionOperations:
    """Delete closed issues or teams with durable audit records."""

    def delete_closed_issue(self, actor: Actor, issue_number: str) -> DeletionResult:
        """Hard-delete one closed issue and persist its queue record atomically."""
        operation_id = uuid4()
        target_id: UUID | None = None
        target_label = issue_number.strip() or "未知问题"
        try:
            with self._session_factory() as session, session.begin():
                admin = self._assert_admin(session, actor)
                case = session.scalar(
                    select(Case).where(Case.case_ref == issue_number.strip()).with_for_update()
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
                team = session.scalar(select(Team).where(Team.id == team_id).with_for_update())
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
                    raise Conflict(f"团队仍被未关闭问题引用，不能删除：{issue_refs}{more}")
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
                    consult_queue_id=(team.id if team.kind is TeamKind.CONSULT_QUEUE else None),
                    dev_team_id=team.id if team.kind is TeamKind.DEV else None,
                )
                session.execute(delete(TeamMembership).where(TeamMembership.team_id == team.id))
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
            tuple(session.scalars(select(CaseEntry).where(CaseEntry.case_id.in_(case_ids))).all())
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
            session_conditions.append(GroupIssueIntakeSession.consult_queue_id == consult_queue_id)
        intake_sessions = (
            tuple(session.scalars(session_statement.where(or_(*session_conditions))).all())
            if session_conditions
            else ()
        )
        intake_session_ids = tuple(record.id for record in intake_sessions)
        for record in intake_sessions:
            media_ids.update(_image_ids(record.parts_json))

        drafts = (
            tuple(
                session.scalars(
                    select(IssueIntakeDraft).where(
                        IssueIntakeDraft.confirmed_issue_id.in_(case_ids)
                    )
                ).all()
            )
            if case_ids
            else ()
        )
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
                delete(DeferredPassiveReply).where(DeferredPassiveReply.case_ref.in_(case_refs))
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
                session.execute(delete(CaseEntryPart).where(CaseEntryPart.entry_id.in_(entry_ids)))
                session.execute(
                    update(CaseEntry)
                    .where(CaseEntry.id.in_(entry_ids))
                    .values(corrects_entry_id=None)
                )
                session.execute(delete(CaseEntry).where(CaseEntry.id.in_(entry_ids)))
            session.execute(update(Case).where(Case.id.in_(case_ids)).values(related_case_id=None))
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
            logger.exception("content_deletion_failure_record_failed operation_id=%s", operation_id)
