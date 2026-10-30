"""Domain routes for the H5 JSON API."""

from __future__ import annotations

import logging
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    FastAPI,
    Request,
)
from fastapi.responses import JSONResponse, Response

from kefu.case_desk.contracts import (
    AdjustCaseDeadline,
    CloseCase,
    PostFormalMessage,
    ReopenCase,
    SetCaseApproachingWindow,
    SetCaseDeadline,
    SetCaseStatus,
    SetIssueCurrentStatus,
    TextPart,
    TransferConsultant,
    TransferDevTeam,
    UpdateCaseMetadata,
)
from kefu.case_desk.errors import (
    CaseDeskError,
    Conflict,
    ValidationError,
)
from kefu.persistence.models import (
    CasePriority,
    CaseStatus,
    EntrySide,
    MessageIntent,
    WaitingOn,
)
from kefu.web.api_models import (
    AdjustIssueDeadlineRequest,
    CloseIssueRequest,
    IssueCommandResponse,
    IssueDetailResponse,
    IssueListResponse,
    PostIssueMessageRequest,
    ReopenIssueRequest,
    SetIssueApproachingWindowRequest,
    SetIssueCurrentStatusRequest,
    SetIssueDeadlineRequest,
    SetIssueWorkflowStatusRequest,
    StatusQueueResponse,
    TransferConsultantRequest,
    TransferDevTeamRequest,
    UpdateIssueMetadataRequest,
)
from kefu.web.runtime import WebRuntime

from .api_support import (
    _as_case_desk_error,
    _can_auto_accept_case,
    _case_filter_for_state,
    _case_json,
    _http_error,
    _issue_command_response,
    _summary_json,
)

logger = logging.getLogger(__name__)


def register_issues_routes(app: FastAPI, runtime: WebRuntime) -> None:
    router = APIRouter()
    storage = runtime.storage
    desk = runtime.desk
    administration = runtime.administration
    issue_management = runtime.issue_management
    actor_or_error = runtime.actor_or_error

    @router.get("/api/issues", response_model=IssueListResponse)
    async def issues_api(
        request: Request,
        state: str = "open",
        priority: str = "",
        q: str = "",
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            page = issue_management.list(_case_filter_for_state(state, priority, q), actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return JSONResponse(
            {
                "items": [_summary_json(item) for item in page.items],
                "offset": page.offset,
                "next_offset": page.next_offset,
            }
        )

    @router.get("/api/status-queue", response_model=StatusQueueResponse)
    async def status_queue_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        active_tasks = desk.list_issue_status_queue()
        active_draft_tasks = desk.list_issue_draft_processing_tasks()
        recent_changes = desk.list_recent_issue_status_changes()
        is_global_admin = administration.is_global_admin(actor)
        recent_deletions = administration.list_content_deletions(actor) if is_global_admin else ()
        return JSONResponse(
            {
                "items": [
                    {
                        "id": str(item.id),
                        "issue_number": item.case_ref,
                        "bug_number": item.bug_number,
                        "customer_name": item.customer_name,
                        "title": item.title,
                        "entry_sequence": item.entry_sequence,
                        "state": item.state,
                        "attempts": item.attempts,
                        "created_at": item.created_at.isoformat(),
                        "next_attempt_at": item.next_attempt_at.isoformat(),
                        "last_error": item.last_error,
                    }
                    for item in active_tasks
                ],
                "draft_tasks": [
                    {
                        "id": str(item.id),
                        "type": "issue_draft",
                        "title": "Agent 整理问题草稿",
                        "state": "processing",
                        "started_at": item.started_at.isoformat(),
                    }
                    for item in active_draft_tasks
                ],
                "recent_status_changes": [
                    {
                        "issue_number": item.case_ref,
                        "bug_number": item.bug_number,
                        "customer_name": item.customer_name,
                        "title": item.title,
                        "field": item.field_label,
                        "from_status": item.from_status,
                        "to_status": item.to_status,
                        "actor_name": item.actor_name,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in recent_changes
                ],
                "recent_deletions": [
                    {
                        "id": str(item.id),
                        "operation_id": str(item.operation_id),
                        "parent_record_id": (
                            str(item.parent_record_id)
                            if item.parent_record_id is not None
                            else None
                        ),
                        "target_kind": item.target_kind.value,
                        "target_id": str(item.target_id) if item.target_id is not None else None,
                        "target_label": item.target_label,
                        "actor_name": item.actor_name,
                        "outcome": item.outcome.value,
                        "error_message": item.error_message,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in recent_deletions
                ],
            }
        )

    @router.get("/api/issues/{issue_number}", response_model=IssueDetailResponse)
    async def issue_api(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            case = issue_management.get(issue_number, actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return JSONResponse(_case_json(case))

    @router.post(
        "/api/issues/{issue_number}/accept",
        response_model=IssueCommandResponse,
    )
    async def accept_issue_api(
        request: Request,
        issue_number: str,
        payload: CloseIssueRequest,
    ) -> IssueCommandResponse:
        actor = actor_or_error(request)
        try:
            case = issue_management.get(issue_number, actor)
            if not _can_auto_accept_case(case):
                return IssueCommandResponse(
                    issue_number=case.issue_number,
                    version=case.version,
                    idempotent=True,
                    accepted=False,
                )
            if case.version != payload.expected_version:
                raise Conflict("问题已被其他人更新，请刷新后重试")
            try:
                result = issue_management.execute(
                    SetCaseStatus(
                        case_ref=case.case_ref,
                        expected_version=payload.expected_version,
                        status=CaseStatus.IN_PROGRESS,
                    ),
                    actor,
                )
            except Conflict:
                refreshed = issue_management.get(issue_number, actor)
                if _can_auto_accept_case(refreshed):
                    raise
                return IssueCommandResponse(
                    issue_number=refreshed.issue_number,
                    version=refreshed.version,
                    idempotent=True,
                    accepted=False,
                )
            return IssueCommandResponse(
                issue_number=result.issue_number or issue_number,
                version=result.version,
                idempotent=result.idempotent,
                accepted=True,
            )
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.post(
        "/api/issues/{issue_number}/messages",
        response_model=IssueCommandResponse,
    )
    async def post_issue_message_api(
        request: Request,
        issue_number: str,
        payload: PostIssueMessageRequest,
    ) -> IssueCommandResponse:
        actor = actor_or_error(request)
        try:
            result = issue_management.execute(
                PostFormalMessage(
                    case_ref=issue_number,
                    expected_version=payload.expected_version,
                    parts=(TextPart(payload.text),),
                    intent=MessageIntent(payload.intent),
                    side=EntrySide(payload.side) if payload.side else None,
                ),
                actor,
            )
            return _issue_command_response(result, issue_number)
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @router.patch(
        "/api/issues/{issue_number}/metadata",
        response_model=IssueCommandResponse,
    )
    async def update_issue_metadata_api(
        request: Request,
        issue_number: str,
        payload: UpdateIssueMetadataRequest,
    ) -> IssueCommandResponse:
        actor = actor_or_error(request)
        try:
            result = issue_management.execute(
                UpdateCaseMetadata(
                    case_ref=issue_number,
                    expected_version=payload.expected_version,
                    customer_name=payload.customer_name,
                    customer_group_name=payload.customer_group_name,
                    customer_contact_name=payload.customer_contact_name,
                    customer_contact_method=payload.customer_contact_method,
                    priority=CasePriority(payload.priority),
                ),
                actor,
            )
            return _issue_command_response(result, issue_number)
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @router.patch(
        "/api/issues/{issue_number}/current-status",
        response_model=IssueCommandResponse,
    )
    async def update_issue_current_status_api(
        request: Request,
        issue_number: str,
        payload: SetIssueCurrentStatusRequest,
    ) -> IssueCommandResponse:
        actor = actor_or_error(request)
        try:
            result = issue_management.execute(
                SetIssueCurrentStatus(
                    case_ref=issue_number,
                    expected_version=payload.expected_version,
                    issue_current_status=payload.issue_current_status,
                ),
                actor,
            )
            return _issue_command_response(result, issue_number)
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.patch(
        "/api/issues/{issue_number}/deadline",
        response_model=IssueCommandResponse,
    )
    async def set_issue_deadline_api(
        request: Request,
        issue_number: str,
        payload: SetIssueDeadlineRequest,
    ) -> IssueCommandResponse:
        actor = actor_or_error(request)
        try:
            deadline = payload.deadline
            if deadline.tzinfo is not None:
                raise ValidationError("截止时间必须使用本地日期时间")
            result = issue_management.execute(
                SetCaseDeadline(
                    case_ref=issue_number,
                    expected_version=payload.expected_version,
                    deadline=deadline.replace(tzinfo=ZoneInfo("Asia/Shanghai")),
                ),
                actor,
            )
            return _issue_command_response(result, issue_number)
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @router.post(
        "/api/issues/{issue_number}/deadline/adjust",
        response_model=IssueCommandResponse,
    )
    async def adjust_issue_deadline_api(
        request: Request,
        issue_number: str,
        payload: AdjustIssueDeadlineRequest,
    ) -> IssueCommandResponse:
        actor = actor_or_error(request)
        try:
            result = issue_management.execute(
                AdjustCaseDeadline(
                    case_ref=issue_number,
                    expected_version=payload.expected_version,
                    adjustment_minutes=payload.adjustment_minutes,
                ),
                actor,
            )
            return _issue_command_response(result, issue_number)
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.patch(
        "/api/issues/{issue_number}/deadline/approaching-window",
        response_model=IssueCommandResponse,
    )
    async def set_issue_approaching_window_api(
        request: Request,
        issue_number: str,
        payload: SetIssueApproachingWindowRequest,
    ) -> IssueCommandResponse:
        actor = actor_or_error(request)
        try:
            result = issue_management.execute(
                SetCaseApproachingWindow(
                    case_ref=issue_number,
                    expected_version=payload.expected_version,
                    approaching_window_minutes=payload.approaching_window_minutes,
                ),
                actor,
            )
            return _issue_command_response(result, issue_number)
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.patch(
        "/api/issues/{issue_number}/status",
        response_model=IssueCommandResponse,
    )
    async def set_issue_workflow_status_api(
        request: Request,
        issue_number: str,
        payload: SetIssueWorkflowStatusRequest,
    ) -> IssueCommandResponse:
        actor = actor_or_error(request)
        try:
            result = issue_management.execute(
                SetCaseStatus(
                    case_ref=issue_number,
                    expected_version=payload.expected_version,
                    status=CaseStatus(payload.status),
                ),
                actor,
            )
            return _issue_command_response(result, issue_number)
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @router.post(
        "/api/issues/{issue_number}/close",
        response_model=IssueCommandResponse,
    )
    async def close_issue_api(
        request: Request,
        issue_number: str,
        payload: CloseIssueRequest,
    ) -> IssueCommandResponse:
        actor = actor_or_error(request)
        try:
            result = issue_management.execute(
                CloseCase(issue_number, payload.expected_version), actor
            )
            return _issue_command_response(result, issue_number)
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.post(
        "/api/issues/{issue_number}/reopen",
        response_model=IssueCommandResponse,
    )
    async def reopen_issue_api(
        request: Request,
        issue_number: str,
        payload: ReopenIssueRequest,
    ) -> IssueCommandResponse:
        actor = actor_or_error(request)
        try:
            result = issue_management.execute(
                ReopenCase(
                    case_ref=issue_number,
                    expected_version=payload.expected_version,
                    waiting_on=WaitingOn.DEV,
                ),
                actor,
            )
            return _issue_command_response(result, issue_number)
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.post(
        "/api/issues/{issue_number}/transfer-consultant",
        response_model=IssueCommandResponse,
    )
    async def transfer_consultant(
        request: Request,
        issue_number: str,
        payload: TransferConsultantRequest,
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            result = issue_management.execute(
                TransferConsultant(
                    case_ref=issue_number,
                    expected_version=payload.expected_version,
                    new_consultant_id=payload.new_consultant_id,
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse(_issue_command_response(result, issue_number).model_dump(mode="json"))

    @router.post(
        "/api/issues/{issue_number}/transfer-dev-team",
        response_model=IssueCommandResponse,
    )
    async def transfer_dev_team(
        request: Request,
        issue_number: str,
        payload: TransferDevTeamRequest,
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            result = issue_management.execute(
                TransferDevTeam(
                    case_ref=issue_number,
                    expected_version=payload.expected_version,
                    new_dev_team_id=payload.new_dev_team_id,
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse(_issue_command_response(result, issue_number).model_dump(mode="json"))

    @router.get("/issues/{issue_number}/media/{media_id}", include_in_schema=False)
    async def issue_media(request: Request, issue_number: str, media_id: UUID) -> Response:
        actor = actor_or_error(request)
        try:
            access = issue_management.get_media_access(issue_number, media_id, actor)
            content = storage.get(access.object_key)
        except (CaseDeskError, OSError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return Response(content=content, media_type=access.mime_type)

    app.include_router(router)
