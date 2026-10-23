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
    CaseStatus,
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
class GroupActor:
    """A side identity established by the active binding for a group chat."""

    side: EntrySide


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
    dev_team_id: UUID
    consult_handler_name: str | None = None
    customer_name: str | None = None
    customer_contact_name: str | None = None
    customer_contact_method: str | None = None
    priority: CasePriority = CasePriority.NORMAL
    parts: tuple[ContentPart, ...] = ()
    source_msgid: str | None = None
    related_case_id: UUID | None = None
    deadline: datetime | None = None
    initial_acceptance_team_id: UUID | None = None
    issue_description: str | None = None
    suspected_type: str | None = None
    issue_current_situation: str | None = None
    issue_current_status: str = "问题未回复"
    issue_source: str | None = None
    issue_metadata: Mapping[str, Any] = field(default_factory=dict)
    approaching_window_minutes: int | None = None
    issue_number: str | None = None
    # Required when a bound consultation group creates the case without an
    # authenticated individual actor. The desk verifies this against the
    # active queue binding before accepting the command.
    origin_chatid: str | None = None


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


@dataclass(frozen=True, slots=True)
class UpdateCaseMetadata:
    case_ref: str
    expected_version: int
    customer_name: str
    customer_contact_name: str | None
    customer_contact_method: str | None
    priority: CasePriority


@dataclass(frozen=True, slots=True)
class SetIssueCurrentStatus:
    case_ref: str
    expected_version: int
    issue_current_status: str


@dataclass(frozen=True, slots=True)
class TransferConsultant:
    case_ref: str
    expected_version: int
    new_consultant_id: UUID


@dataclass(frozen=True, slots=True)
class TransferDevTeam:
    case_ref: str
    expected_version: int
    new_dev_team_id: UUID


@dataclass(frozen=True, slots=True)
class CloseCase:
    case_ref: str
    expected_version: int


@dataclass(frozen=True, slots=True)
class SetCaseStatus:
    case_ref: str
    expected_version: int
    status: CaseStatus


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
    | SetIssueCurrentStatus
    | TransferConsultant
    | TransferDevTeam
    | CloseCase
    | SetCaseStatus
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
    status: CaseStatus
    lifecycle_status: LifecycleStatus
    waiting_on: WaitingOn
    deadline: datetime
    approaching_window_minutes: int
    deadline_status: DeadlineStatus | None
    consult_queue_id: UUID
    consult_queue_name: str
    current_consultant_id: UUID | None
    current_consultant_name: str | None
    consult_handler_name: str
    current_dev_team_id: UUID
    current_dev_team_name: str
    current_dev_team_lead_display_name: str | None
    initial_acceptance_team_id: UUID | None
    initial_acceptance_team_name: str | None
    issue_description: str | None
    suspected_type: str | None
    current_situation: str | None
    issue_current_status: str
    bug_number: str | None
    first_recorder_id: UUID | None
    first_recorder_name: str
    issue_source: str | None
    initial_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int
    can_edit_metadata: bool
    can_change_consult_status: bool
    can_accept_pending: bool
    can_extend_deadline: bool
    can_transfer: bool
    transferable_consultants: tuple[UserOption, ...]
    entries: tuple[EntryView, ...]
    deliveries: tuple[DeliveryView, ...]

    @property
    def issue_number(self) -> str:
        return self.case_ref


@dataclass(frozen=True, slots=True)
class CaseSummary:
    case_ref: str
    title: str
    customer_name: str
    priority: CasePriority
    status: CaseStatus
    lifecycle_status: LifecycleStatus
    waiting_on: WaitingOn
    deadline: datetime
    approaching_window_minutes: int
    deadline_status: DeadlineStatus | None
    current_consultant_id: UUID | None
    updated_at: datetime
    version: int
    can_accept: bool = False

    @property
    def issue_number(self) -> str:
        return self.case_ref


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


IssueView = CaseView
IssueSummary = CaseSummary
IssueOverview = CaseOverview
IssueCommandResult = CommandResult


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
class IssueStatusUpdateClaim:
    id: UUID
    lock_token: UUID
    entry_sequence: int


@dataclass(frozen=True, slots=True)
class IssueStatusUpdateWork:
    case_id: UUID
    id: UUID
    claims: tuple[IssueStatusUpdateClaim, ...]
    case_ref: str
    entry_sequence: int
    trigger_sequences: tuple[int, ...]
    title: str
    issue_description: str | None
    suspected_type: str | None
    current_situation: str | None
    current_status: str
    workflow_status: CaseStatus
    waiting_on: WaitingOn
    timeline: str


@dataclass(frozen=True, slots=True)
class IssueStatusQueueItem:
    id: UUID
    case_ref: str
    title: str
    entry_sequence: int
    state: str
    attempts: int
    created_at: datetime
    next_attempt_at: datetime
    last_error: str | None


@dataclass(frozen=True, slots=True)
class IssueDraftProcessingQueueItem:
    id: UUID
    started_at: datetime


@dataclass(frozen=True, slots=True)
class RecentIssueStatusChange:
    case_ref: str
    title: str
    field_label: str
    from_status: str
    to_status: str
    actor_name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TeamOption:
    id: UUID
    name: str
    lead_display_name: str | None = None


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
