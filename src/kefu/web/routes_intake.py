# ruff: noqa: E501

"""Domain routes for the H5 JSON API."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
)
from fastapi import (
    UploadFile as FastAPIUploadFile,
)
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

from kefu.agent.service import (
    AgentConfigurationError,
    format_customer_issue_title,
)
from kefu.case_desk.contracts import (
    ImagePart,
)
from kefu.case_desk.errors import (
    CaseDeskError,
    Forbidden,
    NotFound,
    ValidationError,
)
from kefu.issues.contracts import IssueFields, IssueIntake
from kefu.issues.service import map_image_placeholders
from kefu.media.storage import (
    MAX_IMAGE_BYTES,
    MediaIngestError,
)
from kefu.observability import opaque_id_fingerprint
from kefu.persistence.models import (
    StoredMedia,
    User,
)
from kefu.web.api_models import (
    ConfirmIssueDraftRequest,
    IssueConfirmedResponse,
    IssueDraftCreatedResponse,
    IssueDraftResponse,
    IssueInspectionRequest,
    IssueInspectionResponse,
    PrivateIntakeOpenRequest,
    PrivateIntakeOpenResponse,
)
from kefu.web.runtime import WebRuntime
from kefu.wecom.customer_group_mapping import customer_name_for_group
from kefu.wecom.customer_groups import WeComCustomerGroupError
from kefu.wecom.transport import InboundImagePart

from .api_support import (
    _PRIVATE_INTAKE_COOKIE,
    _as_case_desk_error,
    _draft_json,
    _http_error,
    _optional_form_text,
    _required_form_text,
)

logger = logging.getLogger(__name__)


def register_intake_routes(app: FastAPI, runtime: WebRuntime) -> None:
    router = APIRouter()
    settings = runtime.settings
    session_factory = runtime.session_factory
    storage = runtime.storage
    wecom_customer_groups = runtime.wecom_customer_groups
    desk = runtime.desk
    media_ingestor = runtime.media_ingestor
    issue_management = runtime.issue_management
    message_receiver = runtime.message_receiver
    inspection = runtime.inspection
    agent = runtime.agent
    external_path = runtime.external_path
    actor_or_error = runtime.actor_or_error

    @router.post(
        "/api/issues/private-intake/open",
        response_model=PrivateIntakeOpenResponse,
    )
    async def open_private_intake(
        request: Request, payload: PrivateIntakeOpenRequest
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            issue_management.get_private_intake_handoff(payload.token, actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        except ValueError as error:
            raise _http_error(ValidationError("私聊交接令牌无效")) from error
        response = JSONResponse({"opened": True}, headers={"Cache-Control": "no-store, private"})
        response.set_cookie(
            _PRIVATE_INTAKE_COOKIE,
            payload.token,
            max_age=2 * 60 * 60,
            path="/",
            secure=urlsplit(settings.web_base_url).scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    @router.get("/api/wecom/agent-js-sdk-config", include_in_schema=False)
    async def agent_js_sdk_config(request: Request, url: str) -> JSONResponse:
        actor = actor_or_error(request)
        del actor  # Authentication keeps this short-lived signature endpoint private.
        try:
            parsed_url = urlsplit(url)
            configured_url = urlsplit(settings.web_base_url)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="JS-SDK 签名页面地址无效") from error
        expected_path = external_path("/issues/new")
        if (
            parsed_url.scheme != configured_url.scheme
            or parsed_url.netloc != configured_url.netloc
            or parsed_url.path != expected_path
            or parsed_url.fragment
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise HTTPException(status_code=400, detail="JS-SDK 签名页面地址无效")
        try:
            config = await asyncio.to_thread(
                wecom_customer_groups.agent_js_sdk_config,
                url,
            )
        except WeComCustomerGroupError as error:
            logger.warning(
                "wecom_agent_js_sdk_config_unavailable reason=%s",
                str(error),
            )
            raise HTTPException(status_code=503, detail="企微 JS-SDK 暂不可用") from error
        return JSONResponse(config, headers={"Cache-Control": "no-store, private"})

    @router.post(
        "/api/issues/inspection/run",
        response_model=IssueInspectionResponse,
    )
    async def run_issue_inspection(
        request: Request,
        payload: IssueInspectionRequest | None = None,
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            result = await asyncio.to_thread(
                inspection.inspect,
                actor=actor,
                team_kinds=payload.team_kinds if payload else None,
            )
            return JSONResponse(
                {
                    "scanned_issues": result.scanned_issues,
                    "summary_messages": result.summary_messages,
                    "unresolved_targets": result.unresolved_targets,
                }
            )
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.post(
        "/api/issues/drafts",
        response_model=IssueDraftCreatedResponse,
        status_code=201,
    )
    async def create_issue_draft(
        request: Request,
        pasted_text: Annotated[str, Form(min_length=1, max_length=50_000)],
        consult_queue_id: Annotated[UUID, Form()],
        source_chat_id: Annotated[str | None, Form(max_length=256)] = None,
        source_chat_status: Annotated[str, Form(max_length=64)] = "not_reported",
        source_chat_error: Annotated[str | None, Form(max_length=256)] = None,
        images: Annotated[list[FastAPIUploadFile] | None, File()] = None,
    ) -> Response:
        request_id = getattr(request.state, "request_id", "untracked")
        actor = actor_or_error(request)
        form = await request.form()
        draft_processing_task_id: UUID | None = None
        private_intake_token = request.cookies.get(_PRIVATE_INTAKE_COOKIE)
        try:
            private_handoff = None
            if private_intake_token:
                existing_draft_id = issue_management.get_existing_private_intake_draft(
                    private_intake_token, actor
                )
                if existing_draft_id is not None:
                    existing_draft = issue_management.get_intake_draft(existing_draft_id, actor)
                    response = JSONResponse(
                        {"draft": _draft_json(existing_draft)},
                        headers={"Cache-Control": "no-store, private"},
                    )
                    response.delete_cookie(_PRIVATE_INTAKE_COOKIE, path="/")
                    return response
                private_handoff = issue_management.get_private_intake_handoff(
                    private_intake_token, actor
                )
            pasted_text = _required_form_text(form, "pasted_text")
            if len(pasted_text) > 50_000:
                raise ValidationError("粘贴内容不能超过50000个字符")
            consult_queue_id = UUID(_required_form_text(form, "consult_queue_id"))
            queues = issue_management.list_consult_queues(actor)
            if consult_queue_id not in {queue.id for queue in queues}:
                raise Forbidden("你不属于所选咨询队列")
            dev_teams = issue_management.list_dev_teams(actor)
            if not dev_teams:
                raise Forbidden("当前没有可用的初次受理组")
            logger.info(
                "issue_draft_inputs_validated request_id=%s actor_fp=%s queue_fp=%s text_chars=%s team_count=%s",
                request_id,
                opaque_id_fingerprint(str(actor.user_id)),
                opaque_id_fingerprint(str(consult_queue_id)),
                len(pasted_text),
                len(dev_teams),
            )
            source_chat_id = (
                None
                if private_handoff is not None
                else source_chat_id or _optional_form_text(form, "source_chat_id")
            )
            reported_group_lookup_status = source_chat_status or _optional_form_text(
                form, "source_chat_status"
            )
            valid_group_lookup_statuses = {
                "unknown",
                "not_reported",
                "not_wecom_client",
                "sdk_script_load_failed",
                "sdk_api_unavailable",
                "js_sdk_config_fetch_failed",
                "js_sdk_config_http_error",
                "js_sdk_config_invalid",
                "agent_config_failed",
                "agent_config_timeout",
                "get_context_failed",
                "not_group_chat_tools",
                "get_cur_external_chat_failed",
                "get_cur_external_chat_timeout",
                "chat_id_missing",
                "success",
                "lookup_exception",
                "lookup_timeout",
                "client_exception",
            }
            if reported_group_lookup_status not in valid_group_lookup_statuses:
                reported_group_lookup_status = "unknown"
            reported_group_lookup_error = source_chat_error or _optional_form_text(
                form, "source_chat_error"
            )
            browser_error_present = reported_group_lookup_error is not None
            safe_group_lookup_error = (
                "".join(char for char in reported_group_lookup_error[:100] if char.isprintable())
                if reported_group_lookup_error
                else "unavailable"
            )
            logger.info(
                "issue_draft_customer_group_lookup_received request_id=%s browser_status=%s "
                "browser_error=%s chat_id_present=%s chat_id_length=%s chatid_fp=%s "
                "browser_error_present=%s",
                request_id,
                reported_group_lookup_status,
                safe_group_lookup_error,
                bool(source_chat_id),
                len(source_chat_id) if source_chat_id else 0,
                opaque_id_fingerprint(source_chat_id) if source_chat_id else "unavailable",
                browser_error_present,
            )
            if source_chat_id and len(source_chat_id) > 256:
                logger.warning(
                    "issue_draft_customer_group_chat_id_rejected request_id=%s reason=too_long length=%s",
                    request_id,
                    len(source_chat_id),
                )
                source_chat_id = None
            source_group_name: str | None = None
            if source_chat_id:
                with session_factory() as session:
                    source_user = session.get(User, actor.user_id)
                    wecom_userid = source_user.wecom_userid if source_user is not None else ""
                try:
                    logger.info(
                        "issue_draft_customer_group_lookup_started request_id=%s chatid_fp=%s "
                        "member_userid_present=%s",
                        request_id,
                        opaque_id_fingerprint(source_chat_id),
                        bool(wecom_userid),
                    )
                    source_group_name = await asyncio.to_thread(
                        wecom_customer_groups.customer_group_name_for_member,
                        source_chat_id,
                        wecom_userid,
                    )
                    logger.info(
                        "issue_draft_customer_group_resolved request_id=%s chatid_fp=%s group_name=%r",
                        request_id,
                        opaque_id_fingerprint(source_chat_id),
                        source_group_name,
                    )
                except WeComCustomerGroupError as error:
                    logger.info(
                        "issue_draft_customer_group_unavailable request_id=%s chatid_fp=%s reason=%s",
                        request_id,
                        opaque_id_fingerprint(source_chat_id),
                        str(error),
                    )
            draft_processing_task_id = desk.start_issue_draft_processing()
            customer_name = customer_name_for_group(source_group_name)
            message_id = uuid4()
            message = message_receiver.receive_workbench(message_id, pasted_text)
            logger.info(
                "issue_draft_source_message_saved request_id=%s message_id_fp=%s",
                request_id,
                opaque_id_fingerprint(str(message_id)),
            )
            try:
                generated = await agent.draft_issue(
                    pasted_text,
                    [team.name for team in dev_teams],
                    request_id=request_id,
                    source_group_name=source_group_name,
                )
            except AgentConfigurationError as error:
                logger.error(
                    "issue_draft_agent_unavailable request_id=%s reason=%s",
                    request_id,
                    str(error),
                )
                raise HTTPException(status_code=503, detail=str(error)) from error
            except RuntimeError as error:
                logger.warning(
                    "issue_draft_agent_failed request_id=%s reason=%s",
                    request_id,
                    str(error),
                )
                raise HTTPException(status_code=502, detail=str(error)) from error

            suggested_group = generated.get("initial_acceptance_group", "").strip()
            selected_team = next(
                (team for team in dev_teams if team.name == suggested_group),
                None,
            )
            logger.info(
                "issue_draft_agent_result_ready request_id=%s field_count=%s team_match=%s",
                request_id,
                sum(bool(value.strip()) for value in generated.values()),
                selected_team is not None,
            )
            logger.info(
                "issue_draft_customer_name_result request_id=%s group_name_present=%s mapping_match=%s customer_name=%r",
                request_id,
                bool(source_group_name),
                bool(customer_name),
                customer_name,
            )
            generated_description = generated.get("issue_description", "")
            if not isinstance(generated_description, str) or not generated_description.strip():
                fallback_description = pasted_text.strip()
                if len(fallback_description) > 20_000:
                    raise ValidationError(
                        "Agent 未能生成问题描述，且原始材料超过20000个字符；请缩短材料后重试"
                    )
                logger.warning(
                    "issue_draft_agent_description_fallback request_id=%s text_chars=%s",
                    request_id,
                    len(fallback_description),
                )
                generated_description = fallback_description
            uploads: list[UploadFile] = []
            form_images = form.getlist("images")
            for item in form_images or (images or ()):
                if isinstance(item, UploadFile):
                    if item.filename:
                        uploads.append(item)
                elif item:
                    raise ValidationError("图片字段格式无效")
            media_ids: list[UUID] = []
            if private_handoff is not None:
                media_ids.extend(
                    part.media_id for part in private_handoff.parts if isinstance(part, ImagePart)
                )
            for upload in uploads:
                image_data = await upload.read(MAX_IMAGE_BYTES + 1)
                if len(image_data) > MAX_IMAGE_BYTES:
                    raise ValidationError("单张图片不能超过 20 MiB")
                media_ids.append(
                    media_ingestor.ingest(
                        InboundImagePart(data=image_data, mime_type=upload.content_type or "")
                    )
                )
            logger.info(
                "issue_draft_media_ingested request_id=%s image_count=%s",
                request_id,
                len(media_ids),
            )
            parts = map_image_placeholders(pasted_text, media_ids)
            fields = IssueFields(
                issue_title=format_customer_issue_title(
                    generated.get("issue_title", ""), customer_name
                ),
                issue_description=generated_description,
                initial_acceptance_team_id=selected_team.id if selected_team else None,
                suspected_type=generated.get("suspected_type", ""),
                current_situation=generated.get("current_situation", ""),
                issue_current_status="问题未回复",
                issue_source="企业微信私聊" if private_handoff is not None else "工作台",
                deadline=datetime.now(ZoneInfo("Asia/Shanghai"))
                + timedelta(hours=settings.issue_default_expiry_hours),
                customer_name=customer_name,
                customer_contact_name=generated.get("customer_contact_name", ""),
                customer_contact_method=generated.get("customer_contact_method", ""),
            )
            draft = issue_management.create_intake_draft(
                IssueIntake(
                    consult_queue_id=consult_queue_id,
                    raw_text=message.text,
                    parts=parts,
                    fields=fields,
                    source_group_name=source_group_name or "",
                ),
                actor,
                private_handoff_token=private_intake_token,
            )
            logger.info(
                "issue_draft_persisted request_id=%s draft_id_fp=%s image_count=%s team_selected=%s",
                request_id,
                opaque_id_fingerprint(str(draft.id)),
                len(media_ids),
                selected_team is not None,
            )
        except MediaIngestError as error:
            raise _http_error(ValidationError(str(error))) from error
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        finally:
            if draft_processing_task_id is not None:
                try:
                    desk.finish_issue_draft_processing(draft_processing_task_id)
                except Exception:
                    logger.exception(
                        "issue_draft_processing_task_cleanup_failed request_id=%s",
                        request_id,
                    )
            await form.close()
        result_draft = issue_management.get_intake_draft(draft.id, actor)
        response = JSONResponse(
            {"draft": _draft_json(result_draft)},
            status_code=201,
            headers={"Cache-Control": "no-store, private"},
        )
        if private_intake_token:
            response.delete_cookie(_PRIVATE_INTAKE_COOKIE, path="/")
        return response

    @router.get("/issues/private-intakes/media/{media_id}", include_in_schema=False)
    async def private_intake_media(request: Request, media_id: UUID) -> Response:
        actor = actor_or_error(request)
        token = request.cookies.get(_PRIVATE_INTAKE_COOKIE)
        if not token:
            raise HTTPException(status_code=404, detail="私聊图片不存在")
        try:
            access = issue_management.get_private_intake_media_access(token, media_id, actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return Response(
            storage.get(access.object_key),
            media_type=access.mime_type,
            headers={"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"},
        )

    @router.get(
        "/api/issues/drafts/{draft_id}",
        response_model=IssueDraftResponse,
    )
    async def issue_draft_api(request: Request, draft_id: UUID) -> dict[str, Any]:
        actor = actor_or_error(request)
        try:
            return _draft_json(issue_management.get_intake_draft(draft_id, actor))
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.get("/issues/drafts/{draft_id}/media/{media_id}", include_in_schema=False)
    async def issue_draft_media(request: Request, draft_id: UUID, media_id: UUID) -> Response:
        actor = actor_or_error(request)
        try:
            draft = issue_management.get_intake_draft(draft_id, actor)
            if not any(
                isinstance(part, ImagePart) and part.media_id == media_id for part in draft.parts
            ):
                raise NotFound("图片附件不存在")
            with session_factory() as session:
                media = session.get(StoredMedia, media_id)
                if media is None:
                    raise NotFound("图片附件不存在")
                object_key, mime_type = media.object_key, media.mime_type
            return Response(storage.get(object_key), media_type=mime_type)
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.post(
        "/api/issues/drafts/{draft_id}/confirm",
        response_model=IssueConfirmedResponse,
    )
    async def confirm_issue_draft_api(
        request: Request,
        draft_id: UUID,
        payload: ConfirmIssueDraftRequest,
    ) -> IssueConfirmedResponse:
        actor = actor_or_error(request)
        try:
            deadline = payload.deadline
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            fields = IssueFields(
                issue_title=payload.issue_title,
                issue_description=payload.issue_description,
                customer_name=payload.customer_name,
                customer_contact_name=payload.customer_contact_name,
                customer_contact_method=payload.customer_contact_method,
                initial_acceptance_team_id=payload.initial_acceptance_team_id,
                suspected_type=payload.suspected_type,
                issue_current_status=payload.issue_current_status,
                current_situation=payload.current_situation,
                issue_source=payload.issue_source,
                deadline=deadline,
            )
            result = issue_management.confirm_intake(draft_id, fields, actor)
            return IssueConfirmedResponse(
                issue_number=result.issue_number,
                version=result.result.version,
                idempotent=result.result.idempotent,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    app.include_router(router)
