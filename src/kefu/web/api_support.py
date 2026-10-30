"""Serialization, validation, and error helpers shared by API routes."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime
from hmac import compare_digest
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import (
    HTTPException,
    Request,
)

from kefu.admin.service import (
    AdminOverview,
    ManagedInspectionSchedule,
    ManagedMembership,
    ManagedTeam,
    ManagedTeamMember,
    ManagedUser,
)
from kefu.case_desk.contracts import (
    CaseFilter,
    ImagePart,
    TextPart,
)
from kefu.case_desk.errors import (
    CaseDeskError,
    Conflict,
    Forbidden,
    NotFound,
    RoutingUnavailable,
    ValidationError,
)
from kefu.config import Settings
from kefu.media.storage import (
    ObjectStorage,
    object_storage_from_settings,
)
from kefu.persistence.models import (
    CasePriority,
    CaseStatus,
    LifecycleStatus,
    WaitingOn,
)
from kefu.web.api_models import (
    IssueCommandResponse,
)
from kefu.web.auth import AuthenticationError

_PRIVATE_INTAKE_COOKIE = "kefu_private_intake"


def _authorize_initial_setup(
    request: Request, token: str, wecom_userid: str, settings: Settings
) -> None:
    configured_token = settings.admin_bootstrap_token
    if configured_token:
        if not compare_digest(token, configured_token):
            raise AuthenticationError("初始化令牌不正确")
        return
    if settings.auth_mode != "development":
        raise AuthenticationError("生产环境必须先配置 ADMIN_BOOTSTRAP_TOKEN")
    # Development mode already uses an explicit userid header.  Requiring it
    # to match the identity being promoted keeps the convenient local flow
    # from becoming a production-safe privilege-escalation pattern.
    header_userid = request.headers.get("X-WeCom-UserId", "").strip()
    if (
        not header_userid
        or not wecom_userid.strip()
        or not compare_digest(header_userid, wecom_userid.strip())
    ):
        raise AuthenticationError("开发模式初始化需要匹配的 X-WeCom-UserId 请求头")


def _admin_overview_json(overview: AdminOverview) -> dict[str, int]:
    return {
        "user_count": overview.user_count,
        "active_user_count": overview.active_user_count,
        "team_count": overview.team_count,
        "consult_queue_count": overview.consult_queue_count,
        "dev_team_count": overview.dev_team_count,
        "initialized_dev_team_count": overview.initialized_dev_team_count,
        "global_admin_count": overview.global_admin_count,
    }


def _managed_membership_json(membership: ManagedMembership) -> dict[str, object]:
    return {
        "team_id": str(membership.team_id),
        "team_name": membership.team_name,
        "team_kind": membership.team_kind.value,
        "role": membership.role.value,
        "active": membership.active,
    }


def _managed_user_json(user: ManagedUser) -> dict[str, object]:
    return {
        "id": str(user.id),
        "wecom_userid": user.wecom_userid,
        "display_name": user.display_name,
        "active": user.active,
        "is_global_admin": user.is_global_admin,
        "memberships": [_managed_membership_json(item) for item in user.memberships],
    }


def _managed_team_member_json(member: ManagedTeamMember) -> dict[str, object]:
    return {
        "user_id": str(member.user_id),
        "display_name": member.display_name,
        "wecom_userid": member.wecom_userid,
        "role": member.role.value,
        "active": member.active,
    }


def _managed_team_json(team: ManagedTeam) -> dict[str, object]:
    return {
        "id": str(team.id),
        "name": team.name,
        "lead_display_name": team.lead_display_name,
        "kind": team.kind.value,
        "active": team.active,
        "channel_chatid": team.channel_chatid,
        "channel_initialized": team.channel_initialized,
        "inspection_age_hours": team.inspection_age_hours,
        "inspection_own_only": team.inspection_own_only,
        "members": [_managed_team_member_json(member) for member in team.members],
    }


def _inspection_schedule_json(schedule: ManagedInspectionSchedule) -> dict[str, object]:
    return {"times": list(schedule.times), "timezone": schedule.timezone}


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


def _storage_from_settings(settings: Settings) -> ObjectStorage:
    return object_storage_from_settings(settings)


def _case_filter_for_state(state: str, priority: str = "", query: str = "") -> CaseFilter:
    base = {
        "mine": CaseFilter(
            lifecycle_status=LifecycleStatus.OPEN,
            assigned_to_me=True,
        ),
        "dev": CaseFilter(waiting_on=WaitingOn.DEV),
        "closed": CaseFilter(lifecycle_status=LifecycleStatus.CLOSED),
        "open": CaseFilter(lifecycle_status=LifecycleStatus.OPEN),
    }.get(state, CaseFilter(lifecycle_status=LifecycleStatus.OPEN))
    try:
        selected_priority = CasePriority(priority) if priority else None
    except ValueError as error:
        raise ValidationError("紧急程度筛选值无效") from error
    return CaseFilter(
        lifecycle_status=base.lifecycle_status,
        waiting_on=base.waiting_on,
        waiting_for_me=base.waiting_for_me,
        assigned_to_me=base.assigned_to_me,
        page_size=base.page_size,
        offset=base.offset,
        priority=selected_priority,
        query=query.strip(),
    )


async def _json_object(request: Request) -> Mapping[str, Any]:
    try:
        body = await request.json()
    except Exception as error:
        raise ValidationError("请求必须是 JSON 对象") from error
    if not isinstance(body, dict):
        raise ValidationError("请求必须是 JSON 对象")
    return body


def _required_form_text(form: Mapping[str, Any], key: str) -> str:
    value = form.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"缺少字段：{key}")
    return value.strip()


def _optional_form_text(form: Mapping[str, Any], key: str) -> str | None:
    value = form.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _as_case_desk_error(error: Exception) -> CaseDeskError:
    if isinstance(error, CaseDeskError):
        return error
    return ValidationError("请求参数无效")


def _http_error(error: CaseDeskError) -> HTTPException:
    status = 422
    if isinstance(error, NotFound):
        status = 404
    elif isinstance(error, Forbidden):
        status = 403
    elif isinstance(error, Conflict):
        status = 409
    elif isinstance(error, (RoutingUnavailable, AuthenticationError)):
        status = 409 if isinstance(error, RoutingUnavailable) else 401
    detail = str(error)
    for old, new in (
        ("研发责任团队", "研发团队"),
        ("咨询队列", "咨询团队"),
        ("咨询经办人", "咨询处理人"),
    ):
        detail = detail.replace(old, new)
    return HTTPException(status_code=status, detail=detail)


def _parse_local_deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValidationError("截止时间格式无效") from error
    if deadline.tzinfo is not None:
        raise ValidationError("截止时间格式无效")
    return deadline.replace(tzinfo=ZoneInfo("Asia/Shanghai"))


def _summary_json(summary: Any) -> dict[str, Any]:
    return {
        "issue_number": summary.issue_number,
        "bug_number": summary.bug_number,
        "title": summary.title,
        "customer_name": summary.customer_name,
        "issue_current_status": summary.issue_current_status,
        "workflow_status": summary.status.value,
        "priority": summary.priority.value,
        "lifecycle_status": summary.lifecycle_status.value,
        "deadline": summary.deadline.isoformat(),
        "approaching_window_minutes": summary.approaching_window_minutes,
        "deadline_status": summary.deadline_status.value if summary.deadline_status else None,
        "updated_at": summary.updated_at.isoformat(),
        "version": summary.version,
        "can_accept": summary.can_accept,
    }


def _issue_command_response(result: Any, issue_number: str) -> IssueCommandResponse:
    return IssueCommandResponse(
        issue_number=result.issue_number or issue_number,
        version=result.version,
        idempotent=result.idempotent,
    )


def _draft_json(draft: Any) -> dict[str, Any]:
    fields = draft.fields
    return {
        "id": str(draft.id),
        "issue_number": draft.issue_number,
        "raw_text": draft.raw_text,
        "parts": [
            {
                "position": position,
                "kind": part.kind.value,
                "text": part.text if isinstance(part, TextPart) else None,
                "media_id": str(part.media_id) if isinstance(part, ImagePart) else None,
            }
            for position, part in enumerate(draft.parts)
        ],
        "consult_queue_id": str(draft.consult_queue_id),
        "fields": {
            "issue_title": fields.issue_title,
            "issue_description": fields.issue_description,
            "customer_name": fields.customer_name,
            "customer_contact_name": fields.customer_contact_name,
            "customer_contact_method": fields.customer_contact_method,
            "initial_acceptance_team_id": (
                str(fields.initial_acceptance_team_id)
                if fields.initial_acceptance_team_id
                else None
            ),
            "suspected_type": fields.suspected_type,
            "issue_current_status": fields.issue_current_status,
            "current_situation": fields.current_situation,
            "issue_source": fields.issue_source,
            "deadline": fields.deadline.isoformat() if fields.deadline else None,
        },
        "first_recorder_name": draft.first_recorder_name,
        "source_group_name": draft.source_group_name,
        "initial_expires_at": draft.initial_expires_at.isoformat(),
        "expires_at": draft.expires_at.isoformat(),
        "confirmed_issue_id": (str(draft.confirmed_issue_id) if draft.confirmed_issue_id else None),
    }


def _can_auto_accept_case(case: Any) -> bool:
    return (
        case.lifecycle_status is LifecycleStatus.OPEN
        and case.status
        in {
            CaseStatus.PENDING_CONFIRMATION,
            CaseStatus.WAITING_CUSTOMER,
            CaseStatus.SUSPENDED,
        }
        and (case.status is not CaseStatus.PENDING_CONFIRMATION or case.can_accept_pending)
    )


def _case_json(case: Any) -> dict[str, Any]:
    deliveries_by_entry: dict[UUID, list[Any]] = {}
    for delivery in case.deliveries:
        deliveries_by_entry.setdefault(delivery.entry_id, []).append(delivery)
    return {
        "issue_number": case.issue_number,
        "title": case.title,
        "issue_description": case.issue_description,
        "initial_acceptance_team_id": (
            str(case.initial_acceptance_team_id) if case.initial_acceptance_team_id else None
        ),
        "initial_acceptance_team_name": case.initial_acceptance_team_name,
        "suspected_type": case.suspected_type,
        "current_situation": case.current_situation,
        "issue_current_status": case.issue_current_status,
        "bug_number": case.bug_number,
        "first_recorder_id": (
            str(case.first_recorder_id) if case.first_recorder_id is not None else None
        ),
        "first_recorder_name": case.first_recorder_name,
        "issue_source": case.issue_source,
        "initial_expires_at": (
            case.initial_expires_at.isoformat() if case.initial_expires_at else None
        ),
        "customer_name": case.customer_name,
        "customer_group_name": case.customer_group_name,
        "customer_contact_name": case.customer_contact_name,
        "customer_contact_method": case.customer_contact_method,
        "priority": case.priority.value,
        "status": case.status.value,
        "lifecycle_status": case.lifecycle_status.value,
        "deadline": case.deadline.isoformat(),
        "approaching_window_minutes": case.approaching_window_minutes,
        "deadline_status": case.deadline_status.value if case.deadline_status else None,
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
        "consult_queue_id": str(case.consult_queue_id),
        "consult_queue_name": case.consult_queue_name,
        "current_consultant_id": (
            str(case.current_consultant_id) if case.current_consultant_id else None
        ),
        "consult_handler_name": case.consult_handler_name,
        "current_consultant_name": case.current_consultant_name,
        "current_dev_team_id": str(case.current_dev_team_id),
        "current_dev_team_name": case.current_dev_team_name,
        "current_dev_team_lead_display_name": case.current_dev_team_lead_display_name,
        "can_transfer": case.can_transfer,
        "transferable_consultants": [
            {"id": str(user.id), "name": user.display_name}
            for user in case.transferable_consultants
        ],
        "can_transfer_dev_team": case.can_transfer_dev_team,
        "transferable_dev_teams": [
            {"id": str(team.id), "name": team.name} for team in case.transferable_dev_teams
        ],
        "can_edit_metadata": case.can_edit_metadata,
        "can_change_consult_status": case.can_change_consult_status,
        "can_accept_pending": case.can_accept_pending,
        "can_extend_deadline": case.can_extend_deadline,
        "version": case.version,
        "entries": [
            {
                "id": str(entry.id),
                "sequence": entry.sequence,
                "kind": entry.kind.value,
                "side": entry.side.value,
                "actor_name": entry.actor_name_snapshot,
                "intent": entry.message_intent.value if entry.message_intent else None,
                "created_at": entry.created_at.isoformat(),
                # waiting_on remains an internal routing value and is never
                # exposed by the new browser API, including historical entries.
                "metadata": {
                    key: value for key, value in entry.metadata.items() if key != "waiting_on"
                },
                "deliveries": [
                    {
                        "destination_type": delivery.destination_type.value,
                        "status": delivery.status.value,
                        "attempts": delivery.attempts,
                    }
                    for delivery in deliveries_by_entry.get(entry.id, ())
                ],
                "parts": [
                    {
                        "position": part.position,
                        "kind": part.kind.value,
                        "text": part.text,
                        "media_id": str(part.media_id) if part.media_id else None,
                    }
                    for part in entry.parts
                ],
            }
            for entry in case.entries
        ],
    }
