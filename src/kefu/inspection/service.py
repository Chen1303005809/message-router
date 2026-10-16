from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import Actor
from kefu.case_desk.errors import CaseDeskError, Forbidden
from kefu.config import Settings
from kefu.messages.contracts import PushTarget
from kefu.messages.receiver import MessageReceiver
from kefu.messages.recipients import (
    RecipientKind,
    RecipientReference,
    RecipientResolver,
)
from kefu.persistence.models import (
    Case,
    IssueInspectionState,
    LifecycleStatus,
    User,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class InspectionResult:
    scanned_issues: int
    periodic_reminders: int
    approaching_reminders: int
    unresolved_targets: int


class IssueInspectionService:
    """Scan every open issue and queue durable reminders through the message module."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        receiver: MessageReceiver,
        recipients: RecipientResolver,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._receiver = receiver
        self._recipients = recipients
        self._settings = settings

    def inspect(
        self, *, now: datetime | None = None, actor: Actor | None = None
    ) -> InspectionResult:
        current = _as_utc(now or utc_now())
        if actor is not None:
            with self._session_factory() as session:
                user = session.get(User, actor.user_id)
                if user is None or not user.active or not user.is_global_admin:
                    raise Forbidden("只有总管理员可以手动运行问题巡检")
        with self._session_factory() as session:
            issues = session.scalars(
                select(Case)
                .where(Case.lifecycle_status == LifecycleStatus.OPEN)
                .order_by(Case.created_at.asc())
            ).all()
            snapshots = tuple(
                (
                    issue.id,
                    issue.case_ref,
                    issue.title,
                    issue.issue_current_status,
                    issue.deadline,
                    issue.approaching_window_minutes,
                    issue.initial_acceptance_team_id or issue.current_dev_team_id,
                    issue.consult_queue_id,
                    issue.created_at,
                )
                for issue in issues
            )

        periodic = approaching = unresolved = 0
        interval = timedelta(hours=self._settings.issue_inspection_interval_hours)
        for (
            issue_id,
            issue_number,
            title,
            status,
            deadline,
            window_minutes,
            acceptance_team_id,
            consult_queue_id,
            created_at,
        ) in snapshots:
            state = self._state(issue_id)
            next_periodic_due = _as_utc(state.last_periodic_sent_at or created_at) + interval
            if next_periodic_due <= current:
                try:
                    target = self._recipients.resolve(
                        RecipientReference(RecipientKind.TEAM, acceptance_team_id)
                    )
                except CaseDeskError:
                    unresolved += 1
                else:
                    result = self._receiver.receive_inspection(
                        message_id=f"periodic:{issue_id}:{next_periodic_due.isoformat()}",
                        target=target,
                        text=_reminder_text(
                            issue_number, title, status, deadline, approaching=False
                        ),
                        idempotency_key=f"inspection:periodic:{issue_id}:{next_periodic_due.isoformat()}",
                    )
                    if not result.queued.idempotent:
                        periodic += 1
                    self._mark_periodic(issue_id, current)

            deadline_utc = _as_utc(deadline)
            entering_soon_window = deadline_utc > current and deadline_utc <= current + timedelta(
                minutes=window_minutes
            )
            if entering_soon_window and state.approaching_notified_at is None:
                references = (
                    RecipientReference(RecipientKind.TEAM, acceptance_team_id),
                    RecipientReference(RecipientKind.CONSULT_QUEUE, consult_queue_id),
                )
                targets: dict[tuple[str, str], PushTarget] = {}
                missing = False
                for reference in references:
                    try:
                        target = self._recipients.resolve(reference)
                    except CaseDeskError:
                        missing = True
                        unresolved += 1
                        continue
                    targets[(target.destination_type.value, target.address)] = target
                message_text = _reminder_text(
                    issue_number, title, status, deadline, approaching=True
                )
                for target in targets.values():
                    key = (
                        f"inspection:approaching:{issue_id}:"
                        f"{target.destination_type.value}:"
                        f"{sha256(target.address.encode()).hexdigest()}"
                    )
                    self._receiver.receive_inspection(
                        message_id=f"approaching:{issue_id}:{target.address}",
                        target=target,
                        text=message_text,
                        idempotency_key=key,
                    )
                    approaching += 1
                if not missing and targets:
                    self._mark_approaching(issue_id, current)

        return InspectionResult(
            scanned_issues=len(snapshots),
            periodic_reminders=periodic,
            approaching_reminders=approaching,
            unresolved_targets=unresolved,
        )

    def _state(self, issue_id: UUID) -> IssueInspectionState:
        with self._session_factory() as session, session.begin():
            state = session.get(IssueInspectionState, issue_id)
            if state is None:
                state = IssueInspectionState(issue_id=issue_id)
                session.add(state)
                session.flush()
            return IssueInspectionState(
                issue_id=state.issue_id,
                last_periodic_sent_at=state.last_periodic_sent_at,
                approaching_notified_at=state.approaching_notified_at,
            )

    def _mark_periodic(self, issue_id: UUID, sent_at: datetime) -> None:
        with self._session_factory() as session, session.begin():
            state = session.get(IssueInspectionState, issue_id)
            if state is None:
                state = IssueInspectionState(issue_id=issue_id)
                session.add(state)
            state.last_periodic_sent_at = sent_at

    def _mark_approaching(self, issue_id: UUID, sent_at: datetime) -> None:
        with self._session_factory() as session, session.begin():
            state = session.get(IssueInspectionState, issue_id)
            if state is None:
                state = IssueInspectionState(issue_id=issue_id)
                session.add(state)
            if state.approaching_notified_at is None:
                state.approaching_notified_at = sent_at


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _reminder_text(
    issue_number: str,
    title: str,
    status: str,
    deadline: datetime,
    *,
    approaching: bool,
) -> str:
    kind = "问题临期提醒" if approaching else "问题巡检提醒"
    return (
        f"**{kind}**\n"
        f"问题编号：〔KF·{issue_number}〕\n"
        f"问题描述：{title}\n"
        f"当前状态：{status}\n"
        f"初次过期时间：{_as_utc(deadline).astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
    )
