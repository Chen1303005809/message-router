"""Commands and views accepted or returned by :class:`CaseDesk`.

The transport and H5 layers translate their own payloads into these immutable
objects.  They do not mutate ORM records directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from kefu.persistence.models import (
    CaseEntryKind,
    CasePriority,
    DeadlineStatus,
    DeliveryDestination,
    DeliveryItemStatus,
    DeliveryStatus,
    EntrySide,
    LifecycleStatus,
    MessageIntent,
    PartKind,
    WaitingOn,
)


@dataclass(frozen=True, slots=True)
class Actor:
    """An authenticated human actor resolved from a stable internal user ID."""

    user_id: UUID


@dataclass(frozen=True, slots=True)
class SystemActor:
    """Reserved actor for delivery bookkeeping; callers cannot impersonate it."""


SYSTEM_ACTOR = SystemActor()


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str
    kind: PartKind = field(default=PartKind.TEXT, init=False)


@dataclass(frozen=True, slots=True)
class ImagePart:
    media_id: UUID
    kind: PartKind = field(default=PartKind.IMAGE, init=False)


type ContentPart = TextPart | ImagePart


@dataclass(frozen=True, slots=True)
class CreateCase:
    title: str
    consult_queue_id: UUID
    developer_id: UUID
    customer_name: str | None = None
    customer_contact_name: str | None = None
    customer_contact_method: str | None = None
    priority: CasePriority = CasePriority.NORMAL
    parts: tuple[ContentPart, ...] = ()
    source_msgid: str | None = None
    draft_id: UUID | None = None
    related_case_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AdjustCaseDeadline:
    case_ref: str
    expected_version: int
    adjustment_minutes: int


@dataclass(frozen=True, slots=True)
class ExtendCaseDeadline:
    case_ref: str
    expected_version: int
    extension_minutes: int


@dataclass(frozen=True, slots=True)
class SetCaseApproachingWindow:
    case_ref: str
    expected_version: int
    approaching_window_minutes: int


@dataclass(frozen=True, slots=True)
class PostFormalMessage:
    case_ref: str
    expected_version: int | None
    parts: tuple[ContentPart, ...]
    intent: MessageIntent = MessageIntent.HANDOFF
    source_msgid: str | None = None
    side: EntrySide | None = None
    origin_chatid: str | None = None
    suppress_delivery: bool = False
    suppress_consult_group_delivery: bool = False


@dataclass(frozen=True, slots=True)
class UpdateCaseMetadata:
    case_ref: str
    expected_version: int
    customer_name: str
    customer_contact_name: str | None
    customer_contact_method: str | None
    priority: CasePriority


@dataclass(frozen=True, slots=True)
class TransferConsultant:
    case_ref: str
    expected_version: int
    new_consultant_id: UUID


@dataclass(frozen=True, slots=True)
class TransferDeveloper:
    case_ref: str
    expected_version: int
    new_developer_id: UUID


@dataclass(frozen=True, slots=True)
class TransferDevTeam:
    case_ref: str
    expected_version: int
    new_dev_team_id: UUID
    new_developer_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CloseCase:
    case_ref: str
    expected_version: int


@dataclass(frozen=True, slots=True)
class ReopenCase:
    case_ref: str
    expected_version: int
    waiting_on: WaitingOn


@dataclass(frozen=True, slots=True)
class CorrectEntry:
    """Append a visible correction instead of moving or rewriting history."""

    case_ref: str
    expected_version: int
    entry_id: UUID
    target_case_ref: str
    target_expected_version: int


@dataclass(frozen=True, slots=True)
class DeliveryItemSucceeded:
    delivery_id: UUID
    item_id: UUID
    lock_token: UUID
    platform_result: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeliveryItemFailed:
    delivery_id: UUID
    item_id: UUID
    lock_token: UUID
    error: str


type Command = (
    CreateCase
    | AdjustCaseDeadline
    | ExtendCaseDeadline
    | SetCaseApproachingWindow
    | PostFormalMessage
    | UpdateCaseMetadata
    | TransferConsultant
    | TransferDeveloper
    | TransferDevTeam
    | CloseCase
    | ReopenCase
    | CorrectEntry
    | DeliveryItemSucceeded
    | DeliveryItemFailed
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    case_ref: str | None
    case_version: int | None
    entry_id: UUID | None = None
    delivery_ids: tuple[UUID, ...] = ()
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class PartView:
    position: int
    kind: PartKind
    text: str | None
    media_id: UUID | None


@dataclass(frozen=True, slots=True)
class EntryView:
    id: UUID
    sequence: int
    kind: CaseEntryKind
    side: EntrySide
    actor_user_id: UUID | None
    actor_name_snapshot: str
    message_intent: MessageIntent | None
    corrects_entry_id: UUID | None
    metadata: Mapping[str, Any]
    created_at: datetime
    parts: tuple[PartView, ...]


@dataclass(frozen=True, slots=True)
class DeliveryView:
    id: UUID
    entry_id: UUID
    destination_type: DeliveryDestination
    destination_address: str
    status: DeliveryStatus
    attempts: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class CaseView:
    id: UUID
    case_ref: str
    title: str
    customer_name: str
    customer_contact_name: str | None
    customer_contact_method: str | None
    priority: CasePriority
    lifecycle_status: LifecycleStatus
    waiting_on: WaitingOn
    deadline: datetime
    approaching_window_minutes: int
    deadline_status: DeadlineStatus | None
    consult_queue_id: UUID
    consult_queue_name: str
    current_consultant_id: UUID | None
    current_consultant_name: str | None
    current_dev_team_id: UUID
    current_dev_team_name: str
    current_developer_id: UUID | None
    current_developer_name: str | None
    created_at: datetime
    updated_at: datetime
    version: int
    can_edit_metadata: bool
    can_extend_deadline: bool
    entries: tuple[EntryView, ...]
    deliveries: tuple[DeliveryView, ...]


@dataclass(frozen=True, slots=True)
class CaseSummary:
    case_ref: str
    title: str
    customer_name: str
    priority: CasePriority
    lifecycle_status: LifecycleStatus
    waiting_on: WaitingOn
    deadline: datetime
    approaching_window_minutes: int
    deadline_status: DeadlineStatus | None
    current_consultant_id: UUID | None
    current_developer_id: UUID | None
    updated_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class CaseOverview:
    open_count: int
    waiting_consult_count: int
    waiting_dev_count: int
    closed_count: int


@dataclass(frozen=True, slots=True)
class CaseFilter:
    lifecycle_status: LifecycleStatus | None = None
    waiting_on: WaitingOn | None = None
    priority: CasePriority | None = None
    waiting_for_me: bool = False
    assigned_to_me: bool = False
    page_size: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[CaseSummary, ...]
    offset: int
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class DeliveryWorkItem:
    id: UUID
    req_id: str
    position: int
    kind: PartKind
    payload: Mapping[str, Any]
    status: DeliveryItemStatus


@dataclass(frozen=True, slots=True)
class DeliveryWork:
    id: UUID
    lock_token: UUID
    destination_type: DeliveryDestination
    destination_address: str
    case_ref: str
    entry_id: UUID
    items: tuple[DeliveryWorkItem, ...]


@dataclass(frozen=True, slots=True)
class DraftView:
    id: UUID
    expires_at: datetime
    parts: tuple[PartView, ...]


@dataclass(frozen=True, slots=True)
class TeamOption:
    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class UserOption:
    id: UUID
    display_name: str
    wecom_userid: str


@dataclass(frozen=True, slots=True)
class MediaAccess:
    id: UUID
    object_key: str
    mime_type: str
