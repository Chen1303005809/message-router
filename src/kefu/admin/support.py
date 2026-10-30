"""Shared value types and helpers for organization management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from kefu.persistence.models import (
    Case,
    DeletionRecordOutcome,
    DeletionTargetKind,
    TeamKind,
)


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
    return (consult_queue_id is not None and consult_reference == str(consult_queue_id)) or (
        dev_team_id is not None and dev_reference == str(dev_team_id)
    )


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
