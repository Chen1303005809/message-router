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
from kefu.persistence.models import Case, LifecycleStatus, Team, TeamKind, User, utc_now


@dataclass(frozen=True, slots=True)
class InspectionResult:
    scanned_issues: int
    summary_messages: int
    unresolved_targets: int


@dataclass(frozen=True, slots=True)
class _IssueSummaryRow:
    bug_number: str
    customer_name: str
    acceptance_team_name: str
    current_dev_team_id: UUID
    consult_queue_id: UUID


class IssueInspectionService:
    """Queue open-issue summaries for development and consultation teams."""

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
            referenced_team_ids = {
                team_id
                for issue in issues
                for team_id in (issue.initial_acceptance_team_id, issue.current_dev_team_id)
                if team_id is not None
            }
            team_names = (
                dict(
                    session.execute(
                        select(Team.id, Team.name).where(Team.id.in_(referenced_team_ids))
                    ).all()
                )
                if referenced_team_ids
                else {}
            )
            active_consult_queue_ids = session.scalars(
                select(Team.id).where(
                    Team.kind == TeamKind.CONSULT_QUEUE,
                    Team.active.is_(True),
                )
            ).all()
            rows: list[_IssueSummaryRow] = []
            for issue in issues:
                if not issue.bug_number:
                    issue.bug_number = next_bug_number(session, _as_utc(issue.created_at))
                assert issue.bug_number is not None
                acceptance_team_name = (
                    team_names.get(issue.initial_acceptance_team_id)
                    if issue.initial_acceptance_team_id is not None
                    else None
                ) or team_names.get(issue.current_dev_team_id) or "未记录"
                rows.append(
                    _IssueSummaryRow(
                        bug_number=issue.bug_number,
                        customer_name=_single_line(issue.customer_name or "未录入") or "未录入",
                        acceptance_team_name=_single_line(acceptance_team_name) or "未记录",
                        current_dev_team_id=issue.current_dev_team_id,
                        consult_queue_id=issue.consult_queue_id,
                    )
                )

        dev_grouped: dict[UUID, list[_IssueSummaryRow]] = defaultdict(list)
        consult_grouped: dict[UUID, list[_IssueSummaryRow]] = {
            queue_id: [] for queue_id in active_consult_queue_ids
        }
        for row in rows:
            dev_grouped[row.current_dev_team_id].append(row)
            consult_grouped.setdefault(row.consult_queue_id, []).append(row)

        summary_messages = unresolved = 0
        for team_id, team_rows in dev_grouped.items():
            queued, failed = self._send_to_recipient(
                RecipientReference(RecipientKind.TEAM, team_id),
                team_rows,
                idempotency_key=f"{idempotency_prefix}:team:{team_id}",
            )
            summary_messages += queued
            unresolved += failed

        for queue_id, queue_rows in consult_grouped.items():
            queued, failed = self._send_to_recipient(
                RecipientReference(RecipientKind.CONSULT_QUEUE, queue_id),
                queue_rows,
                idempotency_key=f"{idempotency_prefix}:consult_queue:{queue_id}",
                empty_summary="暂无未关闭问题",
                include_acceptance_team=True,
            )
            summary_messages += queued
            unresolved += failed

        return InspectionResult(
            scanned_issues=len(rows),
            summary_messages=summary_messages,
            unresolved_targets=unresolved,
        )

    def _send_to_recipient(
        self,
        recipient: RecipientReference,
        rows: list[_IssueSummaryRow],
        *,
        idempotency_key: str,
        empty_summary: str | None = None,
        include_acceptance_team: bool = False,
    ) -> tuple[int, int]:
        try:
            target = self._recipients.resolve(recipient)
        except CaseDeskError:
            return 0, 1

        rows.sort(key=lambda row: row.bug_number)
        content = "\n".join(
            f"**{_single_line(row.customer_name)}** ｜ {_single_line(row.bug_number)}"
            + (
                f"（{_single_line(row.acceptance_team_name)}）"
                if include_acceptance_team
                else ""
            )
            for row in rows
        ) or empty_summary
        if content is None:
            return 0, 0

        result = self._receiver.receive_inspection(
            message_id=idempotency_key,
            target=target,
            text=content,
            idempotency_key=idempotency_key,
        )
        return (0 if result.queued.idempotent else 1), 0


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


