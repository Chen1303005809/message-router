from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import Actor
from kefu.case_desk.errors import CaseDeskError, Forbidden
from kefu.case_desk.numbering import next_bug_number
from kefu.config import Settings
from kefu.inspection.schedule import scheduled_slot
from kefu.messages.receiver import MessageReceiver
from kefu.messages.recipients import (
    RecipientKind,
    RecipientReference,
    RecipientResolver,
)
from kefu.persistence.models import Case, LifecycleStatus, User, utc_now


@dataclass(frozen=True, slots=True)
class InspectionResult:
    scanned_issues: int
    summary_messages: int
    unresolved_targets: int


@dataclass(frozen=True, slots=True)
class _IssueSummaryRow:
    bug_number: str
    customer_name: str
    current_dev_team_id: UUID


class IssueInspectionService:
    """Queue one open-issue summary per current development team."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        receiver: MessageReceiver,
        recipients: RecipientResolver,
        settings: Settings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._receiver = receiver
        self._recipients = recipients
        # Keep accepting Settings for callers configured before inspection moved
        # from interval-based reminders to a fixed daily schedule.
        self._legacy_settings = settings

    def inspect(
        self, *, now: datetime | None = None, actor: Actor | None = None
    ) -> InspectionResult:
        """Send immediately; ``now`` remains accepted for API compatibility."""
        del now
        if actor is not None:
            with self._session_factory() as session:
                user = session.get(User, actor.user_id)
                if user is None or not user.active or not user.is_global_admin:
                    raise Forbidden("只有总管理员可以手动运行问题巡检")

        return self._send_summary(
            idempotency_prefix=f"inspection:manual:{uuid4()}",
        )

    def inspect_scheduled(self, *, now: datetime | None = None) -> InspectionResult:
        """Send a scheduled summary only when ``now`` is one of the fixed slots."""
        slot = scheduled_slot(_as_utc(now or utc_now()))
        if slot is None:
            return InspectionResult(
                scanned_issues=0,
                summary_messages=0,
                unresolved_targets=0,
            )
        return self._send_summary(
            idempotency_prefix=f"inspection:summary:{slot}",
        )

    def _send_summary(self, *, idempotency_prefix: str) -> InspectionResult:
        with self._session_factory() as session, session.begin():
            issues = session.scalars(
                select(Case)
                .where(Case.lifecycle_status == LifecycleStatus.OPEN)
                .order_by(Case.created_at.asc())
                .with_for_update()
            ).all()
            rows: list[_IssueSummaryRow] = []
            for issue in issues:
                if not issue.bug_number:
                    issue.bug_number = next_bug_number(session, _as_utc(issue.created_at))
                assert issue.bug_number is not None
                rows.append(
                    _IssueSummaryRow(
                        bug_number=issue.bug_number,
                        customer_name=_single_line(issue.customer_name or "未录入") or "未录入",
                        current_dev_team_id=issue.current_dev_team_id,
                    )
                )

        grouped: dict[UUID, list[_IssueSummaryRow]] = defaultdict(list)
        for row in rows:
            grouped[row.current_dev_team_id].append(row)

        summary_messages = unresolved = 0
        for team_id, team_rows in grouped.items():
            try:
                target = self._recipients.resolve(
                    RecipientReference(RecipientKind.TEAM, team_id)
                )
            except CaseDeskError:
                unresolved += 1
                continue

            team_rows.sort(key=lambda row: row.bug_number)
            content = "\n".join(
                f"{_single_line(row.bug_number)}｜{_single_line(row.customer_name)}"
                for row in team_rows
            )
            key = f"{idempotency_prefix}:team:{team_id}"
            result = self._receiver.receive_inspection(
                message_id=key,
                target=target,
                text=content,
                idempotency_key=key,
            )
            if not result.queued.idempotent:
                summary_messages += 1

        return InspectionResult(
            scanned_issues=len(rows),
            summary_messages=summary_messages,
            unresolved_targets=unresolved,
        )


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


