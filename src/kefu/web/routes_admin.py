"""Domain routes for the H5 JSON API."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import (
    APIRouter,
    FastAPI,
    Request,
)
from fastapi.responses import JSONResponse

from kefu.case_desk.errors import (
    CaseDeskError,
    ValidationError,
)
from kefu.media.storage import (
    MediaIngestError,
)
from kefu.persistence.models import (
    MembershipRole,
    TeamKind,
)
from kefu.web.api_models import (
    AdminIssueDeleteResponse,
    AdminOverviewResponse,
    AdminTeamDeleteResponse,
    ChannelBindRequest,
    ChannelBindResponse,
    InspectionScheduleResponse,
    InspectionScheduleUpdateRequest,
    ManagedTeamResponse,
    ManagedTeamsResponse,
    ManagedUserResponse,
    ManagedUsersResponse,
    MembershipUpdateRequest,
    TeamCreateRequest,
    TeamInspectionRuleUpdateRequest,
    TeamUpdateRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from kefu.web.runtime import WebRuntime

from .api_support import (
    _admin_overview_json,
    _as_case_desk_error,
    _http_error,
    _inspection_schedule_json,
    _managed_team_json,
    _managed_user_json,
)

logger = logging.getLogger(__name__)


def register_admin_routes(app: FastAPI, runtime: WebRuntime) -> None:
    router = APIRouter()
    administration = runtime.administration
    actor_or_error = runtime.actor_or_error

    @router.get("/api/admin/overview", response_model=AdminOverviewResponse)
    async def admin_overview_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            return JSONResponse(_admin_overview_json(administration.overview(actor)))
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.get(
        "/api/admin/inspection/schedule",
        response_model=InspectionScheduleResponse,
    )
    async def admin_inspection_schedule_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            return JSONResponse(
                _inspection_schedule_json(administration.inspection_schedule(actor))
            )
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.patch(
        "/api/admin/inspection/schedule",
        response_model=InspectionScheduleResponse,
    )
    @router.put(
        "/api/admin/inspection/schedule",
        response_model=InspectionScheduleResponse,
        include_in_schema=False,
    )
    async def admin_update_inspection_schedule_api(
        request: Request, payload: InspectionScheduleUpdateRequest
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            schedule = administration.update_inspection_schedule(
                actor,
                times=payload.times,
            )
            return JSONResponse(_inspection_schedule_json(schedule))
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.get("/api/admin/users", response_model=ManagedUsersResponse)
    async def admin_users_api(request: Request, q: str = "") -> JSONResponse:
        actor = actor_or_error(request)
        try:
            users = administration.list_users(actor, query=q)
            return JSONResponse({"items": [_managed_user_json(user) for user in users]})
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.post("/api/admin/users", response_model=ManagedUserResponse, status_code=201)
    async def admin_create_user_api(request: Request, payload: UserCreateRequest) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            user = administration.create_user(
                actor,
                wecom_userid=payload.wecom_userid,
                display_name=payload.display_name,
                is_global_admin=payload.is_global_admin,
            )
            return JSONResponse(_managed_user_json(user), status_code=201)
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @router.patch("/api/admin/users/{user_id}", response_model=ManagedUserResponse)
    async def admin_update_user_api(
        request: Request, user_id: UUID, payload: UserUpdateRequest
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            user = administration.update_user(
                actor,
                user_id,
                display_name=(
                    payload.display_name if "display_name" in payload.model_fields_set else None
                ),
                active=payload.active if "active" in payload.model_fields_set else None,
                is_global_admin=(
                    payload.is_global_admin
                    if "is_global_admin" in payload.model_fields_set
                    else None
                ),
            )
            return JSONResponse(_managed_user_json(user))
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @router.get("/api/admin/teams", response_model=ManagedTeamsResponse)
    async def admin_teams_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            teams = administration.list_teams(actor)
            return JSONResponse({"items": [_managed_team_json(team) for team in teams]})
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.delete(
        "/api/admin/issues/{issue_number}",
        response_model=AdminIssueDeleteResponse,
    )
    async def admin_delete_issue_api(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            result = administration.delete_closed_issue(actor, issue_number)
            return JSONResponse(
                {
                    "deleted_issue_number": result.deleted_issue_numbers[0],
                    "deletion_record_id": str(result.record_ids[0]),
                    "operation_id": str(result.operation_id),
                }
            )
        except MediaIngestError as error:
            raise _http_error(ValidationError(str(error))) from error
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.delete("/api/admin/teams/{team_id}", response_model=AdminTeamDeleteResponse)
    async def admin_delete_team_api(request: Request, team_id: UUID) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            result = administration.delete_team(actor, team_id)
            return JSONResponse(
                {
                    "deleted_team_id": str(result.deleted_target_id),
                    "deleted_team": result.deleted_target_label,
                    "deleted_issue_numbers": list(result.deleted_issue_numbers),
                    "deletion_record_ids": [str(record_id) for record_id in result.record_ids],
                    "operation_id": str(result.operation_id),
                }
            )
        except MediaIngestError as error:
            raise _http_error(ValidationError(str(error))) from error
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.post("/api/admin/teams", response_model=ManagedTeamResponse, status_code=201)
    async def admin_create_team_api(request: Request, payload: TeamCreateRequest) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            team = administration.create_team(
                actor,
                kind=TeamKind(payload.kind),
                name=payload.name,
                lead_display_name=payload.lead_display_name,
            )
            return JSONResponse(_managed_team_json(team), status_code=201)
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @router.patch("/api/admin/teams/{team_id}", response_model=ManagedTeamResponse)
    async def admin_update_team_api(
        request: Request, team_id: UUID, payload: TeamUpdateRequest
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            team = administration.update_team(
                actor,
                team_id,
                name=payload.name if "name" in payload.model_fields_set else None,
                lead_display_name=(
                    payload.lead_display_name
                    if "lead_display_name" in payload.model_fields_set
                    else None
                ),
                active=payload.active if "active" in payload.model_fields_set else None,
            )
            return JSONResponse(_managed_team_json(team))
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @router.patch(
        "/api/admin/teams/{team_id}/inspection-rule",
        response_model=ManagedTeamResponse,
    )
    async def admin_update_team_inspection_rule_api(
        request: Request,
        team_id: UUID,
        payload: TeamInspectionRuleUpdateRequest,
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            team = administration.update_team_inspection_rule(
                actor,
                team_id=team_id,
                age_hours=payload.age_hours,
                own_only=payload.own_only,
            )
            return JSONResponse(_managed_team_json(team))
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @router.post("/api/admin/teams/{team_id}/members", response_model=ManagedTeamResponse)
    async def admin_set_membership_api(
        request: Request,
        team_id: UUID,
        payload: MembershipUpdateRequest,
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            team = administration.set_membership(
                actor,
                team_id=team_id,
                user_id=payload.user_id,
                role=MembershipRole(payload.role),
            )
            return JSONResponse(_managed_team_json(team))
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @router.delete(
        "/api/admin/teams/{team_id}/members/{user_id}",
        response_model=ManagedTeamResponse,
    )
    async def admin_remove_membership_api(
        request: Request, team_id: UUID, user_id: UUID
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            team = administration.remove_membership(
                actor,
                team_id=team_id,
                user_id=user_id,
            )
            return JSONResponse(_managed_team_json(team))
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.post(
        "/api/admin/teams/{team_id}/channel",
        response_model=ChannelBindResponse,
    )
    async def admin_bind_channel_api(
        request: Request, team_id: UUID, payload: ChannelBindRequest
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            channel = administration.bind_channel(
                actor,
                team_id=team_id,
                chatid=payload.chatid,
            )
            return JSONResponse({"team_id": str(channel.team_id), "chatid": channel.chatid})
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    app.include_router(router)
