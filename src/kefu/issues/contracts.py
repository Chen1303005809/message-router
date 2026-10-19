from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from kefu.case_desk.contracts import (
    AdjustCaseDeadline,
    CaseFilter,
    CaseOverview,
    CaseSummary,
    CaseView,
    CloseCase,
    Command,
    ContentPart,
    CorrectEntry,
    CreateCase,
    ExtendCaseDeadline,
    Page,
    PostFormalMessage,
    ReopenCase,
    SetCaseApproachingWindow,
    SetCaseStatus,
    SetIssueCurrentStatus,
    TransferConsultant,
    TransferDevTeam,
    UpdateCaseMetadata,
)


@dataclass(frozen=True, slots=True)
class IssueFields:
    issue_description: str
    initial_acceptance_team_id: UUID | None = None
    suspected_type: str = ""
    current_situation: str = ""
    issue_current_status: str = "问题未回复"
    issue_source: str = "工作台"
    deadline: datetime | None = None
    issue_title: str = ""


@dataclass(frozen=True, slots=True)
class IssueIntake:
    consult_queue_id: UUID
    raw_text: str
    parts: tuple[ContentPart, ...]
    fields: IssueFields


@dataclass(frozen=True, slots=True)
class IssueIntakeDraftView:
    id: UUID
    issue_number: str
    actor_id: UUID
    raw_text: str
    parts: tuple[ContentPart, ...]
    consult_queue_id: UUID
    fields: IssueFields
    first_recorder_name: str
    initial_expires_at: datetime
    expires_at: datetime
    confirmed_issue_id: UUID | None


@dataclass(frozen=True, slots=True)
class ConfirmedIssue:
    issue_number: str
    result: IssueCommandResult


@dataclass(frozen=True, slots=True)
class IssueCommandResult:
    issue_number: str | None
    version: int | None
    entry_id: UUID | None = None
    delivery_ids: tuple[UUID, ...] = ()
    idempotent: bool = False


# Public issue-oriented names for the program interface. The timeline adapter
# keeps its existing command implementation while callers use issue terms.
CreateIssue = CreateCase
IssueCommand = Command
IssueFilter = CaseFilter
IssuePage = Page
IssueContentPart = ContentPart
IssueOverview = CaseOverview
IssueSummary = CaseSummary
IssueView = CaseView
AdjustIssueDeadline = AdjustCaseDeadline
CloseIssue = CloseCase
CorrectIssueEntry = CorrectEntry
ExtendIssueDeadline = ExtendCaseDeadline
PostIssueMessage = PostFormalMessage
ReopenIssue = ReopenCase
SetIssueApproachingWindow = SetCaseApproachingWindow
SetIssueStatus = SetCaseStatus
SetIssueCurrentStatusCommand = SetIssueCurrentStatus
TransferIssueConsultant = TransferConsultant
TransferIssueDevTeam = TransferDevTeam
UpdateIssueMetadata = UpdateCaseMetadata
