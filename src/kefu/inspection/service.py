from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import Actor
from kefu.case_desk.errors import CaseDeskError, Forbidden, ValidationError
from kefu.case_desk.numbering import next_bug_number
from kefu.config import Settings
from kefu.inspection.schedule import (
    DEFAULT_INSPECTION_TIMES,
    scheduled_slot,
)
from kefu.messages.receiver import MessageReceiver
from kefu.messages.recipients import (
    RecipientKind,
    RecipientReference,
    RecipientResolver,
)
from kefu.persistence.models import (
    Case,
    InspectionSchedule,
    LifecycleStatus,
    Team,
    TeamKind,
    User,
    WeComChannel,
    utc_now,
)


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
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _InspectionTarget:
    id: UUID
    kind: TeamKind
    age_hours: int | None
    own_only: bool


class IssueInspectionService:
    """Queue per-team summaries of open issues using each team's own rules."""

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
        # Retain the old constructor shape for callers built before schedule
        # configuration moved into the database.
        self._legacy_settings = settings

    def daily_times(self) -> tuple[str, ...]:
        """Read the current database schedule, falling back for fresh test DBs."""
        with self._session_factory() as session:
            schedule = session.get(InspectionSchedule, 1)
            if schedule is None:
                return DEFAULT_INSPECTION_TIMES
            from kefu.inspection.schedule import normalize_inspection_times

            return normalize_inspection_times(schedule.times)

    def inspect(
        self,
        *,
        now: datetime | None = None,
        actor: Actor | None = None,
        team_kinds: Iterable[TeamKind | str] | None = None,
    ) -> InspectionResult:
        """Send immediately to selected team types (all current types by default)."""
        current = _as_utc(now or utc_now())
        if actor is not None:
            with self._session_factory() as session:
                user = session.get(User, actor.user_id)
                if user is None or not user.active or not user.is_global_admin:
                    raise Forbidden("只有总管理员可以手动运行问题巡检")

        selected_kinds = _normalize_team_kinds(team_kinds)
        return self._send_summary(
            idempotency_prefix=f"inspection:manual:{uuid4()}",
            now=current,
            team_kinds=selected_kinds,
        )

    def inspect_scheduled(self, *, now: datetime | None = None) -> InspectionResult:
        """Send a scheduled summary only when now is in a configured Shanghai slot."""
        current = _as_utc(now or utc_now())
        slot = scheduled_slot(current, self.daily_times())
        if slot is None:
            return InspectionResult(
                scanned_issues=0,
                summary_messages=0,
                unresolved_targets=0,
            )
        return self._send_summary(
            idempotency_prefix=f"inspection:summary:{slot}",
            now=current,
            team_kinds=tuple(TeamKind),
        )

    def _send_summary(
        self,
        *,
        idempotency_prefix: str,
        now: datetime,
        team_kinds: tuple[TeamKind, ...],
    ) -> InspectionResult:
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
            teams = session.scalars(
                select(Team)
                .where(Team.kind.in_(team_kinds), Team.active.is_(True))
                .order_by(Team.kind.asc(), Team.name.asc())
            ).all()
            active_channels = session.scalars(
                select(WeComChannel).where(
                    WeComChannel.active.is_(True),
                    WeComChannel.initialized_at.is_not(None),
                    WeComChannel.team_id.in_([team.id for team in teams]),
                )
            ).all() if teams else []
            channel_by_team: dict[UUID, list[WeComChannel]] = {}
            for channel in active_channels:
                channel_by_team.setdefault(channel.team_id, []).append(channel)
            # A group is effective only when the resolver can map its team to
            # one non-empty active chat binding.
            teams = [
                team
                for team in teams
                if len(channel_by_team.get(team.id, ())) == 1
                and bool(channel_by_team[team.id][0].chatid.strip())
            ]
            targets = [
                _InspectionTarget(
                    id=team.id,
                    kind=team.kind,
                    age_hours=team.inspection_age_hours,
                    own_only=team.inspection_own_only,
                )
                for team in teams
            ]

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
                        created_at=_as_utc(issue.created_at),
                    )
                )

        summary_messages = unresolved = 0
        for team in targets:
            team_rows = [
                row
                for row in rows
                if _matches_team_rule(row, team, now=now)
            ]
            recipient_kind = (
                RecipientKind.CONSULT_QUEUE
                if team.kind is TeamKind.CONSULT_QUEUE
                else RecipientKind.TEAM
            )
            recipient = RecipientReference(recipient_kind, team.id)
            team_key = (
                "consult_queue"
                if team.kind is TeamKind.CONSULT_QUEUE
                else "team"
            )
            queued, failed = self._send_to_recipient(
                recipient,
                team_rows,
                idempotency_key=f"{idempotency_prefix}:{team_key}:{team.id}",
                include_acceptance_team=team.kind is TeamKind.CONSULT_QUEUE,
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
        ) or "暂无进行中的问题"
        text = f"问题巡检\n{content}"
        result = self._receiver.receive_inspection(
            message_id=idempotency_key,
            target=target,
            text=text,
            idempotency_key=idempotency_key,
        )
        return (0 if result.queued.idempotent else 1), 0


def _normalize_team_kinds(team_kinds: Iterable[TeamKind | str] | None) -> tuple[TeamKind, ...]:
    if team_kinds is None:
        return tuple(TeamKind)
    if isinstance(team_kinds, (str, bytes)):
        raise ValidationError("群聊类型必须是类型列表")
    normalized: set[TeamKind] = set()
    try:
        for kind in team_kinds:
            normalized.add(TeamKind(kind))
    except (TypeError, ValueError) as error:
        raise ValidationError("所选群聊类型无效") from error
    if not normalized:
        raise ValidationError("请至少选择一种群聊类型")
    return tuple(kind for kind in TeamKind if kind in normalized)


def _matches_team_rule(
    row: _IssueSummaryRow,
    team: _InspectionTarget,
    *,
    now: datetime,
) -> bool:
    if team.age_hours is not None:
        try:
            threshold = now - timedelta(hours=team.age_hours)
        except OverflowError:
            return False
        # Strictly greater than N hours: the exact threshold instant is not due.
        if not row.created_at < threshold:
            return False
    if team.own_only:
        if team.kind is TeamKind.DEV:
            return row.current_dev_team_id == team.id
        if team.kind is TeamKind.CONSULT_QUEUE:
            return row.consult_queue_id == team.id
    return True


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
