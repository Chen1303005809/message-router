"""Typed JSON contracts exposed by the H5 API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionResponse(ApiModel):
    authenticated: bool
    setup_required: bool
    auth_mode: Literal["development", "wecom_oauth"]
    user_id: UUID | None = None
    display_name: str | None = None
    is_global_admin: bool = False
    can_inspect: bool = False


class SetupRequest(ApiModel):
    wecom_userid: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)
    token: str = Field(default="", max_length=512)


class SetupResponse(ApiModel):
    initialized: bool


class SetupStatusResponse(ApiModel):
    required: bool


class OptionItem(ApiModel):
    id: UUID
    name: str


class IntakeImage(ApiModel):
    media_id: UUID


class PrivateIntake(ApiModel):
    raw_text: str
    media: list[IntakeImage]
    expires_at: datetime


class IntakeOptionsResponse(ApiModel):
    consult_queues: list[OptionItem]
    dev_teams: list[OptionItem]
    default_deadline: datetime
    source_group_lookup_enabled: bool = False
    private_intake: PrivateIntake | None = None


class PrivateIntakeOpenRequest(ApiModel):
    token: str = Field(min_length=1, max_length=64)


class PrivateIntakeOpenResponse(ApiModel):
    opened: bool


class IssuePartResponse(ApiModel):
    position: int
    kind: Literal["text", "image"]
    text: str | None = None
    media_id: UUID | None = None


class IssueDraftFields(ApiModel):
    issue_title: str
    issue_description: str
    customer_name: str = ""
    customer_contact_name: str = ""
    customer_contact_method: str = ""
    initial_acceptance_team_id: UUID | None = None
    suspected_type: str = ""
    issue_current_status: str = "问题未回复"
    current_situation: str = ""
    issue_source: str = "工作台"
    deadline: datetime | None = None


class IssueDraftResponse(ApiModel):
    id: UUID
    issue_number: str
    raw_text: str
    parts: list[IssuePartResponse]
    consult_queue_id: UUID
    fields: IssueDraftFields
    first_recorder_name: str
    source_group_name: str
    initial_expires_at: datetime
    expires_at: datetime
    confirmed_issue_id: UUID | None = None


class IssueDraftCreatedResponse(ApiModel):
    draft: IssueDraftResponse


class ConfirmIssueDraftRequest(ApiModel):
    issue_title: str = Field(min_length=1, max_length=512)
    issue_description: str = Field(min_length=1, max_length=20_000)
    customer_name: str = Field(default="", max_length=256)
    customer_contact_name: str = Field(default="", max_length=256)
    customer_contact_method: str = Field(default="", max_length=512)
    initial_acceptance_team_id: UUID
    suspected_type: str = Field(min_length=1, max_length=128)
    issue_current_status: str = Field(default="问题未回复", max_length=128)
    current_situation: str = Field(default="", max_length=4_000)
    issue_source: str = Field(default="工作台", max_length=256)
    deadline: datetime


class IssueConfirmedResponse(ApiModel):
    issue_number: str
    version: int | None = None
    idempotent: bool = False


class IssueListItem(ApiModel):
    issue_number: str
    bug_number: str | None = None
    title: str
    customer_name: str
    issue_current_status: str
    workflow_status: str
    priority: str
    lifecycle_status: str
    deadline: datetime
    approaching_window_minutes: int
    deadline_status: str | None = None
    updated_at: datetime
    version: int
    can_accept: bool = False


class IssueListResponse(ApiModel):
    items: list[IssueListItem]
    offset: int
    next_offset: int | None


class IssueOption(ApiModel):
    id: UUID
    name: str


class IssueDeliveryResponse(ApiModel):
    destination_type: str
    status: str
    attempts: int


class IssueEntryResponse(ApiModel):
    id: UUID
    sequence: int
    kind: str
    side: str
    actor_name: str
    intent: str | None = None
    created_at: datetime
    metadata: dict[str, Any]
    deliveries: list[IssueDeliveryResponse]
    parts: list[IssuePartResponse]


class IssueDetailResponse(ApiModel):
    issue_number: str
    title: str
    issue_description: str | None = None
    initial_acceptance_team_id: UUID | None = None
    initial_acceptance_team_name: str | None = None
    suspected_type: str | None = None
    current_situation: str | None = None
    issue_current_status: str
    bug_number: str | None = None
    first_recorder_id: UUID | None = None
    first_recorder_name: str
    issue_source: str | None = None
    initial_expires_at: datetime | None = None
    customer_name: str
    customer_group_name: str
    customer_contact_name: str | None = None
    customer_contact_method: str | None = None
    priority: str
    status: str
    lifecycle_status: str
    deadline: datetime
    approaching_window_minutes: int
    deadline_status: str | None = None
    created_at: datetime
    updated_at: datetime
    consult_queue_id: UUID
    consult_queue_name: str
    current_consultant_id: UUID | None = None
    current_consultant_name: str | None = None
    consult_handler_name: str
    current_dev_team_id: UUID
    current_dev_team_name: str
    current_dev_team_lead_display_name: str | None = None
    can_edit_metadata: bool
    can_change_consult_status: bool
    can_accept_pending: bool
    can_extend_deadline: bool
    can_transfer: bool
    transferable_consultants: list[IssueOption]
    can_transfer_dev_team: bool
    transferable_dev_teams: list[IssueOption]
    version: int
    entries: list[IssueEntryResponse]


class IssueCommandResponse(ApiModel):
    issue_number: str
    version: int | None = None
    idempotent: bool = False
    accepted: bool | None = None


class PostIssueMessageRequest(ApiModel):
    expected_version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=20_000)
    intent: Literal["handoff", "sync"] = "handoff"
    side: Literal["consult", "dev"] | None = None


class UpdateIssueMetadataRequest(ApiModel):
    expected_version: int = Field(ge=1)
    customer_name: str = Field(max_length=256)
    customer_group_name: str | None = Field(default=None, max_length=256)
    customer_contact_name: str | None = Field(default=None, max_length=256)
    customer_contact_method: str | None = Field(default=None, max_length=512)
    priority: Literal["normal", "urgent", "severe"]


class SetIssueCurrentStatusRequest(ApiModel):
    expected_version: int = Field(ge=1)
    issue_current_status: str = Field(min_length=1, max_length=128)


class SetIssueDeadlineRequest(ApiModel):
    expected_version: int = Field(ge=1)
    deadline: datetime


class AdjustIssueDeadlineRequest(ApiModel):
    expected_version: int = Field(ge=1)
    adjustment_minutes: int = Field(ge=-525_600, le=525_600)


class SetIssueApproachingWindowRequest(ApiModel):
    expected_version: int = Field(ge=1)
    approaching_window_minutes: int = Field(ge=0, le=525_600)


class SetIssueWorkflowStatusRequest(ApiModel):
    expected_version: int = Field(ge=1)
    status: Literal[
        "pending_confirmation",
        "waiting_customer",
        "in_progress",
        "suspended",
    ]


class ReopenIssueRequest(ApiModel):
    expected_version: int = Field(ge=1)


class TransferConsultantRequest(ApiModel):
    expected_version: int = Field(ge=1)
    new_consultant_id: UUID


class TransferDevTeamRequest(ApiModel):
    expected_version: int = Field(ge=1)
    new_dev_team_id: UUID


class CloseIssueRequest(ApiModel):
    expected_version: int = Field(ge=1)


class IssueInspectionRequest(ApiModel):
    team_kinds: list[Literal["consult_queue", "dev"]] | None = None


class IssueInspectionResponse(ApiModel):
    scanned_issues: int
    summary_messages: int
    unresolved_targets: int


class IssueStatusTask(ApiModel):
    id: UUID
    issue_number: str
    bug_number: str | None = None
    customer_name: str
    title: str
    entry_sequence: int
    state: str
    attempts: int
    created_at: datetime
    next_attempt_at: datetime
    last_error: str | None = None


class DraftProcessingTask(ApiModel):
    id: UUID
    type: str
    title: str
    state: str
    started_at: datetime


class RecentStatusChange(ApiModel):
    issue_number: str
    bug_number: str | None = None
    customer_name: str
    title: str
    field: str
    from_status: str
    to_status: str
    actor_name: str
    created_at: datetime


class RecentDeletion(ApiModel):
    id: UUID
    operation_id: UUID
    parent_record_id: UUID | None = None
    target_kind: str
    target_id: UUID | None = None
    target_label: str
    actor_name: str
    outcome: str
    error_message: str | None = None
    created_at: datetime


class StatusQueueResponse(ApiModel):
    items: list[IssueStatusTask]
    draft_tasks: list[DraftProcessingTask]
    recent_status_changes: list[RecentStatusChange]
    recent_deletions: list[RecentDeletion]


class AdminOverviewResponse(ApiModel):
    user_count: int
    active_user_count: int
    team_count: int
    consult_queue_count: int
    dev_team_count: int
    initialized_dev_team_count: int
    global_admin_count: int


class ManagedMembershipResponse(ApiModel):
    team_id: UUID
    team_name: str
    team_kind: str
    role: str
    active: bool


class ManagedUserResponse(ApiModel):
    id: UUID
    wecom_userid: str
    display_name: str
    active: bool
    is_global_admin: bool
    memberships: list[ManagedMembershipResponse]


class ManagedUsersResponse(ApiModel):
    items: list[ManagedUserResponse]


class ManagedTeamMemberResponse(ApiModel):
    user_id: UUID
    display_name: str
    wecom_userid: str
    role: str
    active: bool


class ManagedTeamResponse(ApiModel):
    id: UUID
    name: str
    lead_display_name: str | None = None
    kind: str
    active: bool
    channel_chatid: str | None = None
    channel_initialized: bool
    inspection_age_hours: int | None = None
    inspection_own_only: bool
    members: list[ManagedTeamMemberResponse]


class ManagedTeamsResponse(ApiModel):
    items: list[ManagedTeamResponse]


class InspectionScheduleResponse(ApiModel):
    times: list[str]
    timezone: str


class InspectionScheduleUpdateRequest(ApiModel):
    times: list[str] | str


class UserCreateRequest(ApiModel):
    display_name: str = Field(min_length=1, max_length=256)
    wecom_userid: str = Field(min_length=1, max_length=128)
    is_global_admin: bool = False


class UserUpdateRequest(ApiModel):
    display_name: str | None = Field(default=None, max_length=256)
    active: bool | None = None
    is_global_admin: bool | None = None


class TeamCreateRequest(ApiModel):
    kind: Literal["consult_queue", "dev"]
    name: str = Field(min_length=1, max_length=256)
    lead_display_name: str | None = Field(default=None, max_length=256)


class TeamUpdateRequest(ApiModel):
    name: str | None = Field(default=None, max_length=256)
    lead_display_name: str | None = Field(default=None, max_length=256)
    active: bool | None = None


class TeamInspectionRuleUpdateRequest(ApiModel):
    age_hours: int | None = Field(default=None, ge=1, le=8_760)
    own_only: bool


class MembershipUpdateRequest(ApiModel):
    user_id: UUID
    role: Literal["member", "admin"]


class ChannelBindRequest(ApiModel):
    chatid: str = Field(min_length=1, max_length=256)


class ChannelBindResponse(ApiModel):
    team_id: UUID
    chatid: str


class MembershipDeleteResponse(ApiModel):
    team_id: UUID
    user_id: UUID
    removed: bool = True


class DeleteIssueResponse(ApiModel):
    issue_number: str
    deleted: bool = True


class AdminIssueDeleteResponse(ApiModel):
    deleted_issue_number: str
    deletion_record_id: UUID
    operation_id: UUID


class AdminTeamDeleteResponse(ApiModel):
    deleted_team_id: UUID
    deleted_team: str
    deleted_issue_numbers: list[str]
    deletion_record_ids: list[UUID]
    operation_id: UUID


class AgentChatRequest(ApiModel):
    messages: list[dict[str, Any]]


class AgentChatResponse(ApiModel):
    message: str
