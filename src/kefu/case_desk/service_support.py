"""Shared validation and delivery helpers for CaseDesk."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kefu.case_desk.errors import ValidationError
from kefu.persistence.models import (
    MAX_DEADLINE_INCREMENT_MINUTES,
    MIN_DEADLINE_INCREMENT_MINUTES,
    Case,
    CaseEntry,
    DeadlineStatus,
    LifecycleStatus,
)

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
