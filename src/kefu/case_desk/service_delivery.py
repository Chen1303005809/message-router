"""CaseDeskDelivery keeps CaseDesk behavior grouped by responsibility."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased

from kefu.case_desk.contracts import (
    CommandResult,
    DeliveryItemFailed,
    DeliveryItemSucceeded,
    DeliveryWork,
    DeliveryWorkItem,
    IssueStatusUpdateClaim,
    IssueStatusUpdateWork,
)
from kefu.case_desk.errors import Conflict, NotFound, RoutingUnavailable, ValidationError
from kefu.persistence.models import (
    Case,
    CaseEntry,
    CaseEntryKind,
    Delivery,
    DeliveryDestination,
    DeliveryItem,
    DeliveryItemStatus,
    DeliveryStatus,
    EntrySide,
    IssueCurrentStatus,
    IssueStatusUpdateJob,
    IssueStatusUpdateState,
    LifecycleStatus,
    MessageIntent,
    WaitingOn,
)
from kefu.wecom.formatting import SourcePart, build_formal_bundle, build_notice_bundle

from .service_support import (
    DEFAULT_DELIVERY_LEASE,
    DEFAULT_STATUS_UPDATE_LEASE,
    DEFERRED_PASSIVE_COMPLETED_MODE,
    DEFERRED_PASSIVE_PENDING_MODE,
    RETRY_DELAYS,
    STATUS_UPDATE_RETRY_DELAYS,
    _original_speaker_name,
    utc_now,
)


class CaseDeskDelivery:
    """CaseDesk Delivery operations."""

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
                jobs_by_id.get(job_id) is None or jobs_by_id[job_id].lock_token != lock_token
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

    def fail_issue_status_update(self, claims: Sequence[IssueStatusUpdateClaim], error: str) -> int:
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
                    jobs_by_id.get(job_id) is None or jobs_by_id[job_id].lock_token != lock_token
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
