# ruff: noqa: E501

"""Static H5 issue workspace served by the same-origin FastAPI app."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException
from functools import lru_cache
from hmac import compare_digest
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler as default_http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.staticfiles import StaticFiles

from kefu.admin.service import (
    Administration,
    AdminOverview,
    ManagedInspectionSchedule,
    ManagedMembership,
    ManagedTeam,
    ManagedTeamMember,
    ManagedUser,
    initialize_global_admin,
)
from kefu.agent.service import (
    Agent,
    AgentConfigurationError,
    build_tool_registry,
)
from kefu.case_desk.contracts import (
    Actor,
    AdjustCaseDeadline,
    CaseFilter,
    CloseCase,
    CreateCase,
    ImagePart,
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
    Forbidden,
    NotFound,
    RoutingUnavailable,
    ValidationError,
)
from kefu.case_desk.service import CaseDesk
from kefu.config import Settings
from kefu.inspection.service import IssueInspectionService
from kefu.issues.contracts import IssueFields, IssueIntake
from kefu.issues.service import IssueManagement, map_image_placeholders
from kefu.logging_setup import configure_logging
from kefu.media.storage import (
    MAX_IMAGE_BYTES,
    MediaIngestError,
    MediaIngestor,
    ObjectStorage,
    object_storage_from_settings,
)
from kefu.messages.push import MessagePush
from kefu.messages.receiver import MessageReceiver
from kefu.messages.recipients import RecipientResolver
from kefu.observability import database_target_fingerprint, opaque_id_fingerprint
from kefu.persistence.database import create_engine_from_url, create_session_factory
from kefu.persistence.models import (
    MAX_DEADLINE_INCREMENT_MINUTES,
    MIN_DEADLINE_INCREMENT_MINUTES,
    CasePriority,
    CaseStatus,
    EntrySide,
    IssueCurrentStatus,
    IssueModule,
    LifecycleStatus,
    MembershipRole,
    MessageIntent,
    StoredMedia,
    TeamKind,
    User,
    WaitingOn,
)
from kefu.relay.handler import Relay
from kefu.tools.issue_titles import format_customer_issue_title
from kefu.web.auth import AuthenticationError, WebAuthenticator
from kefu.wecom.customer_group_mapping import customer_name_for_group
from kefu.wecom.customer_groups import WeComCustomerGroupClient, WeComCustomerGroupError
from kefu.wecom.transport import InboundImagePart

configure_logging("WEB")
# httpx's built-in INFO log prints full request URLs, including access_token query values.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
_PRIVATE_INTAKE_HEADER = "X-Private-Intake-Token"
_STATIC_DIR = Path(__file__).with_name("static")


@lru_cache(maxsize=8)
def _static_page_content(name: str) -> str:
    return (_STATIC_DIR / name).read_text(encoding="utf-8")


def create_app(
    *,
    session_factory: Callable[[], Session] | None = None,
    settings: Settings | None = None,
    authenticator: WebAuthenticator | None = None,
    storage: ObjectStorage | None = None,
    wecom_customer_groups: WeComCustomerGroupClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    wecom_customer_groups = wecom_customer_groups or WeComCustomerGroupClient(settings)
    database_target_fp = "injected-session-factory"
    if session_factory is None:
        engine = create_engine_from_url(settings.database_url)
        database_target_fp = database_target_fingerprint(engine.url)
        session_factory = create_session_factory(engine)
    desk = CaseDesk(session_factory, web_base_url=settings.web_base_url)
    authenticator = authenticator or WebAuthenticator(session_factory, settings)
    storage = storage or _storage_from_settings(settings)
    administration = Administration(session_factory, storage=storage)
    media_ingestor = MediaIngestor(session_factory, storage)
    issue_management = IssueManagement(
        session_factory,
        desk,
        settings,
        storage=storage,
    )
    message_push = MessagePush(session_factory)
    relay = Relay(session_factory, desk, media_ingestor=media_ingestor)
    message_receiver = MessageReceiver(relay, message_push)
    recipient_resolver = RecipientResolver(session_factory)
    inspection = IssueInspectionService(
        session_factory,
        message_receiver,
        recipient_resolver,
        settings,
    )
    agent_tools = build_tool_registry(
        issue_management,
        inspection,
        message_receiver,
        recipient_resolver,
    )
    agent = Agent(settings, agent_tools)
    if settings.initial_admin_wecom_userid:
        initialize_global_admin(
            session_factory,
            wecom_userid=settings.initial_admin_wecom_userid,
            display_name=settings.initial_admin_display_name,
        )
    logger.info(
        "[GROUP-TRACE] web_app_configured database_target_fp=%s",
        database_target_fp,
    )
    app = FastAPI(title="问题工作台", version="0.1.0")
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="web-static")
    app.state.modules = {
        "issues": issue_management,
        "inspection": inspection,
        "message_receiver": message_receiver,
        "message_push": message_push,
        "agent": agent,
        "agent_tools": agent_tools,
        "recipient_resolver": recipient_resolver,
        "wecom_customer_groups": wecom_customer_groups,
    }

    def external_path(path: str) -> str:
        """Build a browser path that preserves an optional proxy mount prefix."""
        if not path.startswith("/"):
            raise ValueError("H5 路径必须以 / 开头")
        mount_path = settings.web_mount_path
        if not mount_path or path == mount_path or path.startswith(f"{mount_path}/"):
            return path
        return f"{mount_path}{path}"

    def static_page(name: str = "app.html", *, headers: Mapping[str, str] | None = None) -> HTMLResponse:
        page = _static_page_content(name).replace(
            "__KEFU_BASE_PATH__",
            settings.web_mount_path,
        )
        return HTMLResponse(page, headers=dict(headers or {}))

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, error: HTTPException) -> Response:
        if error.status_code != 403:
            return await default_http_exception_handler(request, error)

        route = request.scope.get("route")
        route_path = getattr(route, "path", "")
        accept = request.headers.get("accept", "").lower()
        if route_path.startswith("/api/") or "text/html" not in accept:
            return await default_http_exception_handler(request, error)

        detail = error.detail
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)
        return _browser_forbidden_response(
            request,
            detail or "当前身份无权执行此操作。",
            fallback_path=external_path("/issues"),
            mount_path=settings.web_mount_path,
            headers=error.headers,
        )

    @app.middleware("http")
    async def legacy_event_routes(request: Request, call_next):
        path = request.url.path
        mount_path = settings.web_mount_path
        internal_path = (
            path[len(mount_path) :]
            if mount_path and path.startswith(f"{mount_path}/")
            else path
        )
        if internal_path == "/events" or internal_path.startswith("/events/"):
            new_path = (
                "/issues/new"
                if internal_path == "/events" and request.method == "GET"
                else "/issues" + internal_path[len("/events") :]
            )
            if internal_path == "/events/new" and request.method == "POST":
                status_code = 303
            else:
                status_code = 307
            destination = external_path(new_path)
            if request.url.query:
                destination = f"{destination}?{request.url.query}"
            return RedirectResponse(destination, status_code=status_code)
        if internal_path == "/api/events" or internal_path.startswith("/api/events/"):
            new_path = "/api/issues" + internal_path[len("/api/events") :]
            destination = external_path(new_path)
            if request.url.query:
                destination = f"{destination}?{request.url.query}"
            return RedirectResponse(destination, status_code=307)
        if internal_path in {"/issues/draft", "/api/agent/chat"}:
            request_id = uuid4().hex
            request.state.request_id = request_id
            request_started = time.monotonic()
            logger.info(
                "agent_http_request_started request_id=%s method=%s route=%s",
                request_id,
                request.method,
                internal_path,
            )
            try:
                response = await call_next(request)
            except Exception as error:
                logger.error(
                    "agent_http_request_unhandled request_id=%s method=%s route=%s error_type=%s elapsed_ms=%s",
                    request_id,
                    request.method,
                    internal_path,
                    type(error).__name__,
                    _elapsed_ms(request_started),
                )
                raise
            response.headers["X-Request-ID"] = request_id
            log_method = logger.warning if response.status_code >= 500 else logger.info
            log_method(
                "agent_http_request_finished request_id=%s method=%s route=%s status_code=%s elapsed_ms=%s",
                request_id,
                request.method,
                internal_path,
                response.status_code,
                _elapsed_ms(request_started),
            )
            return response
        return await call_next(request)

    def actor_or_error(request: Request) -> Actor:
        try:
            actor = authenticator.actor_for_request(request)
        except CaseDeskError as error:
            raise _http_error(error) from error
        if actor is None:
            raise HTTPException(status_code=401, detail="需要企业微信登录")
        return actor

    def page_actor_or_login(request: Request) -> Actor | RedirectResponse:
        try:
            actor = authenticator.actor_for_request(request)
        except CaseDeskError as error:
            raise _http_error(error) from error
        if actor is not None:
            return actor
        if settings.auth_mode == "wecom_oauth":
            next_path = external_path(request.url.path)
            if request.url.query:
                next_path = f"{next_path}?{request.url.query}"
            try:
                return authenticator.begin_login(next_path)
            except CaseDeskError as error:
                raise _http_error(error) from error
        raise HTTPException(status_code=401, detail="开发模式需要 X-WeCom-UserId 请求头")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(external_path("/issues/new"), status_code=302)

    @app.get("/auth/login", include_in_schema=False)
    async def login(next: str | None = None) -> RedirectResponse:
        try:
            return authenticator.begin_login(next or external_path("/issues/new"))
        except CaseDeskError as error:
            raise _http_error(error) from error

    @app.get("/auth/callback", include_in_schema=False)
    async def oauth_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
        try:
            return authenticator.complete_login(request, code=code, state=state)
        except CaseDeskError as error:
            raise _http_error(error) from error

    @app.get("/issues/private-intake", response_class=HTMLResponse, include_in_schema=False)
    async def private_intake_landing() -> Response:
        return static_page(
            "private-intake.html",
            headers={"Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer"},
        )

    @app.get(
        "/issues/private-intake/continue",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def private_intake_continue(request: Request) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        return static_page(
            "private-intake.html",
            headers={"Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer"},
        )

    @app.post("/api/issues/private-intake/open", include_in_schema=False)
    async def open_private_intake(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            payload = await _json_object(request)
            token = _payload_text(payload, "token")
            if len(token) > 64:
                raise ValidationError("私聊交接令牌无效")
            issue_management.get_private_intake_handoff(token, actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        except ValueError as error:
            raise _http_error(ValidationError("私聊交接令牌无效")) from error
        return JSONResponse(
            {"ok": True},
            headers={"Cache-Control": "no-store, private"},
        )

    @app.get("/api/wecom/agent-js-sdk-config", include_in_schema=False)
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

    @app.on_event("shutdown")
    async def close_agent_client() -> None:
        await agent.close()

    @app.get("/setup", response_class=HTMLResponse, include_in_schema=False)
    async def setup_page() -> Response:
        """One-time visual bootstrap for an instance without a global admin."""
        try:
            if administration.has_global_admin():
                return RedirectResponse(external_path("/admin"), status_code=303)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return static_page()

    @app.post("/setup", include_in_schema=False)
    async def setup_submit(request: Request) -> RedirectResponse:
        try:
            if administration.has_global_admin():
                return RedirectResponse(external_path("/admin"), status_code=303)
            form = await _urlencoded_form(request)
            _authorize_initial_setup(
                request,
                form.get("token", ""),
                form.get("wecom_userid", ""),
                settings,
            )
            initialize_global_admin(
                session_factory,
                wecom_userid=_required(form, "wecom_userid"),
                display_name=_required(form, "display_name"),
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path("/admin"), status_code=303)

    @app.get("/api/setup", include_in_schema=False)
    async def setup_status_api() -> JSONResponse:
        try:
            initialized = administration.has_global_admin()
        except CaseDeskError as error:
            raise _http_error(error) from error
        return JSONResponse(
            {
                "initialized": initialized,
                "requires_token": bool(settings.admin_bootstrap_token),
                "auth_mode": settings.auth_mode,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/setup", include_in_schema=False)
    async def setup_api(request: Request) -> JSONResponse:
        try:
            if administration.has_global_admin():
                raise Conflict("总管理员已经初始化")
            payload = await _json_object(request)
            userid = _payload_text(payload, "wecom_userid")
            display_name = _payload_text(payload, "display_name")
            _authorize_initial_setup(
                request,
                _payload_text(payload, "token") if "token" in payload else "",
                userid,
                settings,
            )
            initialize_global_admin(
                session_factory,
                wecom_userid=userid,
                display_name=display_name,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"ok": True}, status_code=201)

    @app.get("/api/ui/context", include_in_schema=False)
    async def ui_context_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            queues = issue_management.list_consult_queues(actor)
            access = inspection.access_for_actor(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return JSONResponse(
            {
                "user_id": str(actor.user_id),
                "is_global_admin": access.is_global_admin,
                "can_inspect": access.can_inspect,
                "can_create_issue": bool(queues),
                "wecom_group_context_enabled": wecom_customer_groups.is_configured,
                "modules": [item.value for item in IssueModule],
                "issue_statuses": [item.value for item in IssueCurrentStatus],
            },
            headers={"Cache-Control": "no-store, private"},
        )

    @app.get("/api/issues/intake-options", include_in_schema=False)
    async def intake_options_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            private_token = (
                _private_intake_token(request)
                if _private_intake_requested(request)
                else None
            )
            queues = issue_management.list_consult_queues(actor)
            dev_teams = issue_management.list_dev_teams(actor)
            private_handoff = (
                issue_management.get_private_intake_handoff(private_token, actor)
                if private_token
                else None
            )
            access = inspection.access_for_actor(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        private_images = []
        private_text = ""
        if private_handoff is not None:
            private_text = private_handoff.raw_text
            private_images = [
                {
                    "media_id": str(part.media_id),
                    "url": external_path(f"/issues/private-intakes/media/{part.media_id}"),
                }
                for part in private_handoff.parts
                if isinstance(part, ImagePart)
            ]
        return JSONResponse(
            {
                "queues": [{"id": str(item.id), "name": item.name} for item in queues],
                "dev_teams": [
                    {
                        "id": str(team.id),
                        "name": team.name,
                        "lead_display_name": team.lead_display_name,
                    }
                    for team in dev_teams
                ],
                "private_intake": (
                    {
                        "raw_text": private_text,
                        "images": private_images,
                    }
                    if private_handoff is not None
                    else None
                ),
                "can_inspect": access.can_inspect,
                "is_global_admin": access.is_global_admin,
                "wecom_group_context_enabled": (
                    wecom_customer_groups.is_configured and private_handoff is None
                ),
                "modules": [item.value for item in IssueModule],
                "issue_statuses": [item.value for item in IssueCurrentStatus],
            },
            headers={"Cache-Control": "no-store, private"},
        )

    @app.get("/api/issues/legacy-intake-options", include_in_schema=False)
    async def legacy_intake_options_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            queues = issue_management.list_consult_queues(actor)
            dev_teams = issue_management.list_dev_teams(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return JSONResponse(
            {
                "queues": [{"id": str(item.id), "name": item.name} for item in queues],
                "dev_teams": [
                    {
                        "id": str(team.id),
                        "name": team.name,
                        "lead_display_name": team.lead_display_name,
                    }
                    for team in dev_teams
                ],
                "modules": [item.value for item in IssueModule],
                "issue_statuses": [item.value for item in IssueCurrentStatus],
            },
            headers={"Cache-Control": "no-store, private"},
        )

    @app.get("/issues/new", response_class=HTMLResponse)
    async def new_issue_intake_page(request: Request) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        private_intake_requested = _private_intake_requested(request)
        try:
            queues = issue_management.list_consult_queues(actor)
            dev_teams = issue_management.list_dev_teams(actor)
            issue_management.cleanup_expired_drafts()
        except CaseDeskError as error:
            raise _http_error(error) from error
        if not queues:
            return RedirectResponse(
                f"{external_path('/issues')}?{urlencode({'notice': 'missing_consult_queue'})}",
                status_code=302,
            )
        if not dev_teams:
            return RedirectResponse(
                f"{external_path('/issues')}?{urlencode({'notice': 'missing_dev_team'})}",
                status_code=302,
            )
        headers = (
            {"Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer"}
            if private_intake_requested
            else None
        )
        return static_page(headers=headers)

    @app.post("/api/issues/inspection/run")
    async def run_issue_inspection(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            raw_body = await request.body()
            payload = await _json_object(request) if raw_body.strip() else {}
            team_kinds = payload.get("team_kinds")
            result = await asyncio.to_thread(
                inspection.inspect,
                actor=actor,
                team_kinds=team_kinds,
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

    @app.post("/api/issues/drafts", include_in_schema=False)
    @app.post("/issues/draft", include_in_schema=False)
    async def create_issue_draft(request: Request) -> Response:
        api_request = request.url.path == "/api/issues/drafts"
        request_id = getattr(request.state, "request_id", "untracked")
        actor = actor_or_error(request) if api_request else page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await request.form()
        draft_processing_task_id: UUID | None = None
        private_intake_token = None
        try:
            if _private_intake_requested(request):
                private_intake_token = _private_intake_token(request)
            private_handoff = None
            if private_intake_token:
                existing_draft_id = issue_management.get_existing_private_intake_draft(
                    private_intake_token, actor
                )
                if existing_draft_id is not None:
                    response: Response
                    if api_request:
                        response = JSONResponse({"draft_id": str(existing_draft_id)})
                    else:
                        response = RedirectResponse(
                            external_path(f"/issues/drafts/{existing_draft_id}"),
                            status_code=303,
                        )
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
                else _optional_form_text(form, "source_chat_id")
            )
            reported_group_lookup_status = _optional_form_text(form, "source_chat_status")
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
            reported_group_lookup_error = _optional_form_text(form, "source_chat_error")
            browser_error_present = reported_group_lookup_error is not None
            safe_group_lookup_error = (
                "".join(
                    char
                    for char in reported_group_lookup_error[:100]
                    if char.isprintable()
                )
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
            for item in form.getlist("images"):
                if isinstance(item, UploadFile):
                    if item.filename:
                        uploads.append(item)
                elif item:
                    raise ValidationError("图片字段格式无效")
            media_ids: list[UUID] = []
            if private_handoff is not None:
                media_ids.extend(
                    part.media_id
                    for part in private_handoff.parts
                    if isinstance(part, ImagePart)
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
        response: Response
        if api_request:
            response = JSONResponse({"draft_id": str(draft.id)}, status_code=201)
        else:
            response = RedirectResponse(
                external_path(f"/issues/drafts/{draft.id}"),
                status_code=303,
            )
        return response

    @app.get("/issues/private-intakes/media/{media_id}", include_in_schema=False)
    async def private_intake_media(request: Request, media_id: UUID) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        token = request.headers.get(_PRIVATE_INTAKE_HEADER, "").strip()
        if not token:
            raise HTTPException(status_code=404, detail="私聊图片不存在")
        if len(token) > 64:
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

    @app.get("/issues/drafts/{draft_id}", response_class=HTMLResponse)
    async def issue_draft_review(request: Request, draft_id: UUID) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            issue_management.get_intake_draft(draft_id, actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return static_page(headers={"Cache-Control": "no-store, private"})

    @app.get("/api/issues/drafts/{draft_id}", include_in_schema=False)
    async def issue_draft_api(request: Request, draft_id: UUID) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            draft = issue_management.get_intake_draft(draft_id, actor)
            queues = issue_management.list_consult_queues(actor)
            dev_teams = issue_management.list_dev_teams(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        queue_name = next(
            (queue.name for queue in queues if queue.id == draft.consult_queue_id),
            "已登记咨询队列",
        )
        parts = []
        for part in draft.parts:
            if isinstance(part, TextPart):
                parts.append({"kind": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                parts.append(
                    {
                        "kind": "image",
                        "media_id": str(part.media_id),
                        "url": external_path(
                            f"/issues/drafts/{draft.id}/media/{part.media_id}"
                        ),
                    }
                )
        fields = draft.fields
        return JSONResponse(
            {
                "id": str(draft.id),
                "issue_number": draft.issue_number,
                "raw_text": draft.raw_text,
                "parts": parts,
                "consult_queue_id": str(draft.consult_queue_id),
                "consult_queue_name": queue_name,
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
                    "deadline": _datetime_local_value(
                        fields.deadline or draft.initial_expires_at
                    ),
                },
                "source_group_name": draft.source_group_name,
                "first_recorder_name": draft.first_recorder_name,
                "initial_expires_at": _datetime_local_value(draft.initial_expires_at),
                "dev_teams": [
                    {"id": str(team.id), "name": team.name}
                    for team in dev_teams
                ],
                "modules": [item.value for item in IssueModule],
                "issue_statuses": [item.value for item in IssueCurrentStatus],
            },
            headers={"Cache-Control": "no-store, private"},
        )

    @app.get("/issues/drafts/{draft_id}/media/{media_id}", include_in_schema=False)
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

    @app.post("/api/issues/drafts/{draft_id}/confirm", include_in_schema=False)
    @app.post("/issues/drafts/{draft_id}/confirm", include_in_schema=False)
    async def confirm_issue_draft(request: Request, draft_id: UUID) -> Response:
        api_request = request.url.path == f"/api/issues/drafts/{draft_id}/confirm"
        actor = actor_or_error(request) if api_request else page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _json_object(request) if api_request else await request.form()
        try:
            fields = IssueFields(
                issue_title=(
                    _payload_text(form, "issue_title")
                    if api_request
                    else _required_form_text(form, "issue_title")
                ),
                issue_description=(
                    _payload_text(form, "issue_description")
                    if api_request
                    else _required_form_text(form, "issue_description")
                ),
                customer_name=(
                    _payload_text(form, "customer_name")
                    if api_request
                    else _optional_form_text(form, "customer_name") or ""
                ),
                customer_contact_name=(
                    _payload_text(form, "customer_contact_name")
                    if api_request
                    else _optional_form_text(form, "customer_contact_name") or ""
                ),
                customer_contact_method=(
                    _payload_text(form, "customer_contact_method")
                    if api_request
                    else _optional_form_text(form, "customer_contact_method") or ""
                ),
                initial_acceptance_team_id=UUID(
                    _payload_text(form, "initial_acceptance_team_id")
                    if api_request
                    else _required_form_text(form, "initial_acceptance_team_id")
                ),
                suspected_type=(
                    _payload_text(form, "suspected_type")
                    if api_request
                    else _optional_form_text(form, "suspected_type") or ""
                ),
                issue_current_status=(
                    _payload_text(form, "issue_current_status")
                    if api_request
                    else _required_form_text(form, "issue_current_status")
                ),
                current_situation=(
                    _payload_text(form, "current_situation")
                    if api_request
                    else _optional_form_text(form, "current_situation") or ""
                ),
                issue_source=(
                    _payload_text(form, "issue_source")
                    if api_request
                    else _optional_form_text(form, "issue_source") or "工作台"
                ),
                deadline=_parse_local_deadline(
                    _payload_text(form, "deadline")
                    if api_request
                    else _required_form_text(form, "deadline")
                ),
            )
            result = issue_management.confirm_intake(draft_id, fields, actor)
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        finally:
            if not api_request:
                await form.close()
        if api_request:
            return JSONResponse({"issue_number": result.issue_number}, status_code=201)
        return RedirectResponse(
            external_path(f"/issues/{result.issue_number}"),
            status_code=303,
        )

    @app.post("/api/agent/chat")
    async def agent_chat_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or not all(
            isinstance(item, Mapping) for item in raw_messages
        ):
            raise HTTPException(status_code=422, detail="messages 必须是消息对象数组")
        try:
            reply = await agent.chat(
                raw_messages,
                actor,
                request_id=getattr(request.state, "request_id", "untracked"),
            )
        except AgentConfigurationError as error:
            logger.error(
                "agent_chat_unavailable request_id=%s reason=%s",
                getattr(request.state, "request_id", "untracked"),
                str(error),
            )
            raise HTTPException(status_code=503, detail=str(error)) from error
        except RuntimeError as error:
            logger.warning(
                "agent_chat_failed request_id=%s reason=%s",
                getattr(request.state, "request_id", "untracked"),
                str(error),
            )
            raise HTTPException(status_code=502, detail=str(error)) from error
        return JSONResponse({"message": reply})

    @app.get("/settings/routing", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request, q: str = "") -> Response:
        del q
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            administration.overview(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return static_page()

    @app.get("/api/admin/overview")
    async def admin_overview_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            return JSONResponse(_admin_overview_json(administration.overview(actor)))
        except CaseDeskError as error:
            raise _http_error(error) from error

    @app.get("/api/admin/inspection/schedule")
    async def admin_inspection_schedule_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            return JSONResponse(_inspection_schedule_json(administration.inspection_schedule(actor)))
        except CaseDeskError as error:
            raise _http_error(error) from error

    @app.patch("/api/admin/inspection/schedule")
    @app.put("/api/admin/inspection/schedule")
    async def admin_update_inspection_schedule_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            schedule = administration.update_inspection_schedule(
                actor,
                times=payload.get("times"),
            )
            return JSONResponse(_inspection_schedule_json(schedule))
        except CaseDeskError as error:
            raise _http_error(error) from error

    @app.get("/api/admin/users")
    async def admin_users_api(request: Request, q: str = "") -> JSONResponse:
        actor = actor_or_error(request)
        try:
            users = administration.list_users(actor, query=q)
            return JSONResponse({"items": [_managed_user_json(user) for user in users]})
        except CaseDeskError as error:
            raise _http_error(error) from error

    @app.post("/api/admin/users")
    async def admin_create_user_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            user = administration.create_user(
                actor,
                wecom_userid=_payload_text(payload, "wecom_userid"),
                display_name=_payload_text(payload, "display_name"),
                is_global_admin=_payload_bool(payload, "is_global_admin", default=False),
            )
            return JSONResponse(_managed_user_json(user), status_code=201)
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @app.patch("/api/admin/users/{user_id}")
    async def admin_update_user_api(request: Request, user_id: UUID) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            user = administration.update_user(
                actor,
                user_id,
                display_name=(
                    _payload_text(payload, "display_name") if "display_name" in payload else None
                ),
                active=(_payload_bool(payload, "active") if "active" in payload else None),
                is_global_admin=(
                    _payload_bool(payload, "is_global_admin")
                    if "is_global_admin" in payload
                    else None
                ),
            )
            return JSONResponse(_managed_user_json(user))
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @app.get("/api/admin/teams")
    async def admin_teams_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            teams = administration.list_teams(actor)
            return JSONResponse({"items": [_managed_team_json(team) for team in teams]})
        except CaseDeskError as error:
            raise _http_error(error) from error

    @app.delete("/api/admin/issues/{issue_number}")
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

    @app.delete("/api/admin/teams/{team_id}")
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

    @app.post("/api/admin/teams")
    async def admin_create_team_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            team = administration.create_team(
                actor,
                kind=TeamKind(str(payload["kind"])),
                name=_payload_text(payload, "name"),
                lead_display_name=(
                    _payload_text(payload, "lead_display_name")
                    if "lead_display_name" in payload
                    else None
                ),
            )
            return JSONResponse(_managed_team_json(team), status_code=201)
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @app.patch("/api/admin/teams/{team_id}")
    async def admin_update_team_api(request: Request, team_id: UUID) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            team = administration.update_team(
                actor,
                team_id,
                name=_payload_text(payload, "name") if "name" in payload else None,
                lead_display_name=(
                    _payload_text(payload, "lead_display_name")
                    if "lead_display_name" in payload
                    else None
                ),
                active=_payload_bool(payload, "active") if "active" in payload else None,
            )
            return JSONResponse(_managed_team_json(team))
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @app.patch("/api/admin/teams/{team_id}/inspection-rule")
    async def admin_update_team_inspection_rule_api(
        request: Request, team_id: UUID
    ) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            age_hours = payload.get("age_hours")
            if age_hours is not None and not isinstance(age_hours, int):
                raise ValidationError("巡检小时数必须是正整数")
            team = administration.update_team_inspection_rule(
                actor,
                team_id=team_id,
                age_hours=age_hours,
                own_only=_payload_bool(payload, "own_only"),
            )
            return JSONResponse(_managed_team_json(team))
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @app.post("/api/admin/teams/{team_id}/members")
    async def admin_set_membership_api(request: Request, team_id: UUID) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            team = administration.set_membership(
                actor,
                team_id=team_id,
                user_id=UUID(str(payload["user_id"])),
                role=MembershipRole(str(payload.get("role", MembershipRole.MEMBER.value))),
            )
            return JSONResponse(_managed_team_json(team))
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @app.delete("/api/admin/teams/{team_id}/members/{user_id}")
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

    @app.post("/api/admin/teams/{team_id}/channel")
    async def admin_bind_channel_api(request: Request, team_id: UUID) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            channel = administration.bind_channel(
                actor,
                team_id=team_id,
                chatid=_payload_text(payload, "chatid"),
            )
            return JSONResponse({"team_id": str(channel.team_id), "chatid": channel.chatid})
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    # The same operations are available as regular forms so an operator can
    # complete the whole setup from a mobile H5 browser without a JS client.
    @app.post("/admin/users", include_in_schema=False)
    async def admin_create_user_form(request: Request) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            form = await _urlencoded_form(request)
            administration.create_user(
                actor,
                wecom_userid=_required(form, "wecom_userid"),
                display_name=_required(form, "display_name"),
                is_global_admin=form.get("is_global_admin") == "on",
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path("/admin"), status_code=303)

    @app.post("/admin/users/{user_id}", include_in_schema=False)
    async def admin_update_user_form(request: Request, user_id: UUID) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            form = await _urlencoded_form(request)
            administration.update_user(
                actor,
                user_id,
                display_name=_required(form, "display_name"),
                active=form.get("active") == "on",
                is_global_admin=form.get("is_global_admin") == "on",
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path("/admin"), status_code=303)

    @app.post("/admin/teams", include_in_schema=False)
    async def admin_create_team_form(request: Request) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            form = await _urlencoded_form(request)
            administration.create_team(
                actor,
                kind=TeamKind(_required(form, "kind")),
                name=_required(form, "name"),
                lead_display_name=_optional_form_text(form, "lead_display_name"),
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path("/admin"), status_code=303)

    @app.post("/admin/teams/{team_id}", include_in_schema=False)
    async def admin_update_team_form(request: Request, team_id: UUID) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            form = await _urlencoded_form(request)
            administration.update_team(
                actor,
                team_id,
                name=_required(form, "name"),
                lead_display_name=_optional_form_text(form, "lead_display_name"),
                active=form.get("active") == "on",
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path("/admin"), status_code=303)

    @app.post("/admin/teams/{team_id}/delete", include_in_schema=False)
    async def admin_delete_team_form(request: Request, team_id: UUID) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            administration.delete_team(actor, team_id)
        except MediaIngestError as error:
            raise _http_error(ValidationError(str(error))) from error
        except CaseDeskError as error:
            raise _http_error(error) from error
        return RedirectResponse(external_path("/admin"), status_code=303)

    @app.post("/admin/teams/{team_id}/members", include_in_schema=False)
    async def admin_set_membership_form(request: Request, team_id: UUID) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            form = await _urlencoded_form(request)
            administration.set_membership(
                actor,
                team_id=team_id,
                user_id=UUID(_required(form, "user_id")),
                role=MembershipRole(_required(form, "role")),
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path("/admin"), status_code=303)

    @app.post("/admin/teams/{team_id}/members/{user_id}/remove", include_in_schema=False)
    async def admin_remove_membership_form(
        request: Request, team_id: UUID, user_id: UUID
    ) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            administration.remove_membership(actor, team_id=team_id, user_id=user_id)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return RedirectResponse(external_path("/admin"), status_code=303)

    @app.post("/admin/teams/{team_id}/channel", include_in_schema=False)
    async def admin_bind_channel_form(request: Request, team_id: UUID) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            form = await _urlencoded_form(request)
            administration.bind_channel(
                actor,
                team_id=team_id,
                chatid=_required(form, "chatid"),
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path("/admin"), status_code=303)

    @app.post("/admin/inspection/schedule", include_in_schema=False)
    async def admin_update_inspection_schedule_form(request: Request) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            form = await _urlencoded_form(request)
            administration.update_inspection_schedule(actor, times=form.get("times", ""))
        except CaseDeskError as error:
            raise _http_error(error) from error
        return RedirectResponse(external_path("/admin"), status_code=303)

    @app.post("/admin/teams/{team_id}/inspection-rule", include_in_schema=False)
    async def admin_update_team_inspection_rule_form(
        request: Request, team_id: UUID
    ) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            form = await _urlencoded_form(request)
            age_hours = (
                int(_required(form, "inspection_age_hours"))
                if form.get("inspection_age_limited") == "on"
                else None
            )
            own_only_text = form.get("inspection_own_only")
            if own_only_text not in {"true", "false"}:
                raise ValidationError("问题归属条件无效")
            administration.update_team_inspection_rule(
                actor,
                team_id=team_id,
                age_hours=age_hours,
                own_only=own_only_text == "true",
            )
        except ValueError as error:
            raise _http_error(ValidationError("巡检小时数必须是正整数")) from error
        except CaseDeskError as error:
            raise _http_error(error) from error
        return RedirectResponse(external_path("/admin"), status_code=303)

    @app.get("/issues", response_class=HTMLResponse)
    async def events_page(request: Request) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        return static_page()

    @app.get("/api/issues")
    async def issues_api(
        request: Request,
        state: str = "open",
        priority: str = "",
        q: str = "",
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            page = issue_management.list(
                _case_filter_for_state(state, priority, q), actor
            )
        except CaseDeskError as error:
            raise _http_error(error) from error
        return JSONResponse(
            {
                "items": [_summary_json(item) for item in page.items],
                "offset": page.offset,
                "next_offset": page.next_offset,
            }
        )

    @app.get("/status-queue", response_class=HTMLResponse)
    async def status_queue_page(request: Request) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        return static_page()

    @app.get("/api/status-queue")
    async def status_queue_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        active_tasks = desk.list_issue_status_queue()
        active_draft_tasks = desk.list_issue_draft_processing_tasks()
        recent_changes = desk.list_recent_issue_status_changes()
        is_global_admin = administration.is_global_admin(actor)
        recent_deletions = (
            administration.list_content_deletions(actor) if is_global_admin else ()
        )
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

    @app.get("/legacy/events/new", response_class=HTMLResponse, include_in_schema=False)
    async def new_case_form_page(request: Request) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        return static_page()

    @app.post("/api/issues", include_in_schema=False)
    @app.post("/legacy/events/new", include_in_schema=False)
    async def create_case_from_h5(request: Request) -> Response:
        api_request = request.url.path == "/api/issues"
        actor = actor_or_error(request) if api_request else page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await request.form()
        try:
            title = _required_form_text(form, "title")
            description = _required_form_text(form, "description")
            customer_name = _required_form_text(form, "customer_name")
            customer_group_name = _optional_form_text(form, "customer_group_name")
            consult_handler_name = _required_form_text(form, "consult_handler_name")
            customer_contact_name = _optional_form_text(form, "customer_contact_name")
            customer_contact_method = _optional_form_text(form, "customer_contact_method")
            consult_queue_id = UUID(_required_form_text(form, "consult_queue_id"))
            dev_team_id = UUID(_required_form_text(form, "dev_team_id"))
            suspected_type = _required_form_text(form, "suspected_type")
            issue_current_status = _required_form_text(form, "issue_current_status")
            priority = CasePriority(_optional_form_text(form, "priority") or "normal")
            deadline = _parse_local_deadline(_optional_form_text(form, "deadline"))
            if len(title) > 512:
                raise ValidationError("问题标题不能超过 512 个字符")
            if len(description) > 20_000:
                raise ValidationError("问题描述不能超过 20000 个字符")
            if len(customer_name) > 256:
                raise ValidationError("客户 / 企业名称不能超过 256 个字符")
            if customer_group_name and len(customer_group_name) > 256:
                raise ValidationError("客户群聊名称不能超过 256 个字符")
            if len(consult_handler_name) > 256:
                raise ValidationError("咨询经办人不能超过 256 个字符")
            if customer_contact_name and len(customer_contact_name) > 256:
                raise ValidationError("联系人不能超过 256 个字符")
            if customer_contact_method and len(customer_contact_method) > 256:
                raise ValidationError("联系方式不能超过 256 个字符")

            queues = issue_management.list_consult_queues(actor)
            if consult_queue_id not in {queue.id for queue in queues}:
                raise Forbidden("你不属于所选咨询团队")
            dev_teams = issue_management.list_dev_teams(actor)
            if dev_team_id not in {team.id for team in dev_teams}:
                raise Forbidden("所选研发处理团队不可用")

            uploads: list[UploadFile] = []
            for item in form.getlist("images"):
                if isinstance(item, UploadFile):
                    if item.filename:
                        uploads.append(item)
                elif item:
                    raise ValidationError("图片字段格式无效")

            parts: list[TextPart | ImagePart] = [TextPart(description)]
            ingestor = MediaIngestor(session_factory, storage)
            for upload in uploads:
                image_data = await upload.read(MAX_IMAGE_BYTES + 1)
                if len(image_data) > MAX_IMAGE_BYTES:
                    raise ValidationError("单张图片不能超过 20 MiB")
                media_id = ingestor.ingest(
                    InboundImagePart(
                        data=image_data,
                        mime_type=upload.content_type or "",
                    )
                )
                parts.append(ImagePart(media_id))

            result = issue_management.execute(
                CreateCase(
                    title=title,
                    consult_queue_id=consult_queue_id,
                    dev_team_id=dev_team_id,
                    consult_handler_name=consult_handler_name,
                    customer_name=customer_name,
                    customer_group_name=customer_group_name,
                    customer_contact_name=customer_contact_name,
                    customer_contact_method=customer_contact_method,
                    priority=priority,
                    deadline=deadline,
                    issue_description=description,
                    suspected_type=suspected_type,
                    issue_current_status=issue_current_status,
                    parts=tuple(parts),
                ),
                actor,
            )
        except MediaIngestError as error:
            raise _http_error(ValidationError(str(error))) from error
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        finally:
            await form.close()
        assert result.issue_number is not None
        if api_request:
            return JSONResponse({"issue_number": result.issue_number}, status_code=201)
        return RedirectResponse(external_path(f"/issues/{result.issue_number}"), status_code=303)

    @app.get("/issues/{issue_number}", response_class=HTMLResponse)
    async def issue_detail(request: Request, issue_number: str) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            case = issue_management.get(issue_number, actor)
            if (
                case.lifecycle_status is LifecycleStatus.OPEN
                and case.status is CaseStatus.PENDING_CONFIRMATION
                and case.can_accept_pending
            ):
                try:
                    issue_management.execute(
                        SetCaseStatus(
                            case_ref=case.case_ref,
                            expected_version=case.version,
                            status=CaseStatus.IN_PROGRESS,
                        ),
                        actor,
                    )
                except Conflict:
                    pass
        except CaseDeskError as error:
            raise _http_error(error) from error
        return static_page()

    @app.get("/api/issues/{issue_number}")
    async def issue_api(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            case = issue_management.get(issue_number, actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return JSONResponse(_case_json(case))

    @app.post("/issues/{issue_number}/messages", include_in_schema=False)
    async def post_message(request: Request, issue_number: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            intent = MessageIntent(form.get("intent", MessageIntent.HANDOFF.value))
            side = EntrySide(form["side"]) if form.get("side") else None
            issue_management.execute(
                PostFormalMessage(
                    case_ref=issue_number,
                    expected_version=int(_required(form, "version")),
                    parts=(TextPart(_required(form, "text")),),
                    intent=intent,
                    side=side,
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/issues/{escape(issue_number)}"), status_code=303)

    @app.post("/issues/{issue_number}/metadata", include_in_schema=False)
    async def update_issue_metadata(request: Request, issue_number: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            issue_management.execute(
                UpdateCaseMetadata(
                    case_ref=issue_number,
                    expected_version=int(_required(form, "version")),
                    customer_name=_required(form, "customer_name"),
                    customer_group_name=form.get("customer_group_name"),
                    customer_contact_name=form.get("customer_contact_name"),
                    customer_contact_method=form.get("customer_contact_method"),
                    priority=CasePriority(_required(form, "priority")),
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/issues/{escape(issue_number)}"), status_code=303)

    @app.post("/issues/{issue_number}/issue-current-status", include_in_schema=False)
    async def update_issue_current_status(
        request: Request, issue_number: str
    ) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            issue_management.execute(
                SetIssueCurrentStatus(
                    case_ref=issue_number,
                    expected_version=int(_required(form, "version")),
                    issue_current_status=_required(form, "issue_current_status"),
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/issues/{escape(issue_number)}"), status_code=303)

    @app.post("/issues/{issue_number}/deadline/adjust", include_in_schema=False)
    @app.post("/issues/{issue_number}/deadline/extend", include_in_schema=False)
    async def adjust_issue_deadline(request: Request, issue_number: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            if "deadline_date" in form or "deadline_time" in form:
                deadline_value = (
                    f"{_required(form, 'deadline_date')}T"
                    f"{_required(form, 'deadline_time')}"
                )
                deadline = _parse_local_deadline(deadline_value)
                if deadline is None:
                    raise ValidationError("请填写截止日期和时间")
                issue_management.execute(
                    SetCaseDeadline(
                        case_ref=issue_number,
                        expected_version=int(_required(form, "version")),
                        deadline=deadline,
                    ),
                    actor,
                )
            else:
                adjustment_field = (
                    "adjustment_hours" if "adjustment_hours" in form else "extension_hours"
                )
                adjustment_minutes = _half_hour_hours_to_minutes(
                    _required(form, adjustment_field), "调整时长"
                )
                direction = form.get("direction", "delay")
                if direction == "advance":
                    adjustment_minutes = -adjustment_minutes
                elif direction != "delay":
                    raise ValidationError("期限调整方向无效")
                issue_management.execute(
                    AdjustCaseDeadline(
                        case_ref=issue_number,
                        expected_version=int(_required(form, "version")),
                        adjustment_minutes=adjustment_minutes,
                    ),
                    actor,
                )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/issues/{escape(issue_number)}"), status_code=303)

    @app.post("/issues/{issue_number}/deadline/approaching-window", include_in_schema=False)
    async def set_issue_approaching_window(request: Request, issue_number: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            issue_management.execute(
                SetCaseApproachingWindow(
                    case_ref=issue_number,
                    expected_version=int(_required(form, "version")),
                    approaching_window_minutes=_half_hour_hours_to_minutes(
                        _required(form, "approaching_hours"), "临近期限提醒时间"
                    ),
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/issues/{escape(issue_number)}"), status_code=303)

    @app.post("/issues/{issue_number}/open", include_in_schema=False)
    async def open_issue_from_list(request: Request, issue_number: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            case = issue_management.get(issue_number, actor)
            if _can_auto_accept_case(case):
                try:
                    issue_management.execute(
                        SetCaseStatus(
                            case_ref=case.case_ref,
                            expected_version=case.version,
                            status=CaseStatus.IN_PROGRESS,
                        ),
                        actor,
                    )
                except Conflict:
                    case = issue_management.get(issue_number, actor)
                    if _can_auto_accept_case(case):
                        issue_management.execute(
                            SetCaseStatus(
                                case_ref=case.case_ref,
                                expected_version=case.version,
                                status=CaseStatus.IN_PROGRESS,
                            ),
                            actor,
                        )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/issues/{escape(issue_number)}"), status_code=303)

    @app.post("/issues/{issue_number}/status", include_in_schema=False)
    async def set_issue_status(request: Request, issue_number: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            issue_management.execute(
                SetCaseStatus(
                    case_ref=issue_number,
                    expected_version=int(_required(form, "version")),
                    status=CaseStatus(_required(form, "status")),
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/issues/{escape(issue_number)}"), status_code=303)

    @app.post("/issues/{issue_number}/close", include_in_schema=False)
    async def close_issue(request: Request, issue_number: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            issue_management.execute(CloseCase(issue_number, int(_required(form, "version"))), actor)
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/issues/{escape(issue_number)}"), status_code=303)

    @app.post("/issues/{issue_number}/delete", include_in_schema=False)
    async def delete_closed_issue_form(
        request: Request, issue_number: str
    ) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            administration.delete_closed_issue(actor, issue_number)
        except MediaIngestError as error:
            raise _http_error(ValidationError(str(error))) from error
        except CaseDeskError as error:
            raise _http_error(error) from error
        return RedirectResponse(external_path("/issues?state=closed"), status_code=303)

    @app.post("/issues/{issue_number}/reopen", include_in_schema=False)
    async def reopen_issue(request: Request, issue_number: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            issue_management.execute(
                ReopenCase(
                    case_ref=issue_number,
                    expected_version=int(_required(form, "version")),
                    waiting_on=WaitingOn(_required(form, "waiting_on")),
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/issues/{escape(issue_number)}"), status_code=303)

    @app.post("/issues/{issue_number}/transfer-consultant", include_in_schema=False)
    async def transfer_consultant_page(request: Request, issue_number: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            issue_management.execute(
                TransferConsultant(
                    case_ref=issue_number,
                    expected_version=int(_required(form, "version")),
                    new_consultant_id=UUID(_required(form, "new_consultant_id")),
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/issues/{escape(issue_number)}"), status_code=303)

    @app.post("/api/issues/{issue_number}/transfer-consultant")
    async def transfer_consultant(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            result = issue_management.execute(
                TransferConsultant(
                    case_ref=issue_number,
                    expected_version=int(payload["version"]),
                    new_consultant_id=UUID(str(payload["new_consultant_id"])),
                ),
                actor,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"issue_number": result.issue_number, "version": result.version})

    @app.post("/issues/{issue_number}/transfer-dev-team", include_in_schema=False)
    async def transfer_dev_team_page(request: Request, issue_number: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            issue_management.execute(
                TransferDevTeam(
                    case_ref=issue_number,
                    expected_version=int(_required(form, "version")),
                    new_dev_team_id=UUID(_required(form, "new_dev_team_id")),
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/issues/{escape(issue_number)}"), status_code=303)

    @app.post("/api/issues/{issue_number}/transfer-dev-team")
    async def transfer_dev_team(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            result = issue_management.execute(
                TransferDevTeam(
                    case_ref=issue_number,
                    expected_version=int(payload["version"]),
                    new_dev_team_id=UUID(str(payload["new_dev_team_id"])),
                ),
                actor,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"issue_number": result.issue_number, "version": result.version})

    @app.post("/api/issues/{issue_number}/messages")
    async def post_message_api(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            side_value = payload.get("side")
            result = issue_management.execute(
                PostFormalMessage(
                    case_ref=issue_number,
                    expected_version=int(payload["version"]),
                    parts=(TextPart(_payload_text(payload, "text")),),
                    intent=MessageIntent(
                        payload.get("intent", MessageIntent.HANDOFF.value)
                    ),
                    side=EntrySide(str(side_value)) if side_value else None,
                ),
                actor,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"issue_number": result.issue_number, "version": result.version})

    @app.patch("/api/issues/{issue_number}/metadata")
    async def update_issue_metadata_api(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            result = issue_management.execute(
                UpdateCaseMetadata(
                    case_ref=issue_number,
                    expected_version=int(payload["version"]),
                    customer_name=_payload_text(payload, "customer_name"),
                    customer_group_name=_payload_text(payload, "customer_group_name"),
                    customer_contact_name=_payload_text(payload, "customer_contact_name"),
                    customer_contact_method=_payload_text(payload, "customer_contact_method"),
                    priority=CasePriority(_payload_text(payload, "priority")),
                ),
                actor,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"issue_number": result.issue_number, "version": result.version})

    @app.patch("/api/issues/{issue_number}/issue-current-status")
    async def update_issue_current_status_api(
        request: Request, issue_number: str
    ) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            result = issue_management.execute(
                SetIssueCurrentStatus(
                    case_ref=issue_number,
                    expected_version=int(payload["version"]),
                    issue_current_status=_payload_text(payload, "issue_current_status"),
                ),
                actor,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"issue_number": result.issue_number, "version": result.version})

    @app.patch("/api/issues/{issue_number}/deadline")
    async def update_issue_deadline_api(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            version = int(payload["version"])
            deadline_text = payload.get("deadline")
            if deadline_text is not None:
                if not isinstance(deadline_text, str):
                    raise ValidationError("截止时间格式无效")
                deadline = _parse_local_deadline(deadline_text)
                if deadline is None:
                    raise ValidationError("请填写截止日期和时间")
                command = SetCaseDeadline(
                    case_ref=issue_number,
                    expected_version=version,
                    deadline=deadline,
                )
            else:
                adjustment_minutes = _half_hour_hours_to_minutes(
                    _payload_text(payload, "adjustment_hours"), "调整时长"
                )
                direction = payload.get("direction", "delay")
                if direction == "advance":
                    adjustment_minutes = -adjustment_minutes
                elif direction != "delay":
                    raise ValidationError("期限调整方向无效")
                command = AdjustCaseDeadline(
                    case_ref=issue_number,
                    expected_version=version,
                    adjustment_minutes=adjustment_minutes,
                )
            result = issue_management.execute(command, actor)
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"issue_number": result.issue_number, "version": result.version})

    @app.patch("/api/issues/{issue_number}/deadline/approaching-window")
    async def update_issue_approaching_window_api(
        request: Request, issue_number: str
    ) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            result = issue_management.execute(
                SetCaseApproachingWindow(
                    case_ref=issue_number,
                    expected_version=int(payload["version"]),
                    approaching_window_minutes=_half_hour_hours_to_minutes(
                        _payload_text(payload, "approaching_hours"), "临近期限提醒时间"
                    ),
                ),
                actor,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"issue_number": result.issue_number, "version": result.version})

    @app.post("/api/issues/{issue_number}/open")
    async def open_issue_api(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            case = issue_management.get(issue_number, actor)
            if _can_auto_accept_case(case):
                try:
                    issue_management.execute(
                        SetCaseStatus(
                            case_ref=case.case_ref,
                            expected_version=case.version,
                            status=CaseStatus.IN_PROGRESS,
                        ),
                        actor,
                    )
                except Conflict:
                    case = issue_management.get(issue_number, actor)
                    if _can_auto_accept_case(case):
                        issue_management.execute(
                            SetCaseStatus(
                                case_ref=case.case_ref,
                                expected_version=case.version,
                                status=CaseStatus.IN_PROGRESS,
                            ),
                            actor,
                        )
            case = issue_management.get(issue_number, actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return JSONResponse({"issue_number": case.issue_number, "version": case.version})

    @app.patch("/api/issues/{issue_number}/status")
    async def set_issue_status_api(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            result = issue_management.execute(
                SetCaseStatus(
                    case_ref=issue_number,
                    expected_version=int(payload["version"]),
                    status=CaseStatus(_payload_text(payload, "status")),
                ),
                actor,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"issue_number": result.issue_number, "version": result.version})

    @app.post("/api/issues/{issue_number}/close")
    async def close_issue_api(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            result = issue_management.execute(
                CloseCase(issue_number, int(payload["version"])), actor
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"issue_number": result.issue_number, "version": result.version})

    @app.post("/api/issues/{issue_number}/reopen")
    async def reopen_issue_api(request: Request, issue_number: str) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            result = issue_management.execute(
                ReopenCase(
                    case_ref=issue_number,
                    expected_version=int(payload["version"]),
                    waiting_on=WaitingOn(_payload_text(payload, "waiting_on")),
                ),
                actor,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"issue_number": result.issue_number, "version": result.version})

    @app.get("/issues/{issue_number}/media/{media_id}", include_in_schema=False)
    async def issue_media(request: Request, issue_number: str, media_id: UUID) -> Response:
        actor = actor_or_error(request)
        try:
            access = issue_management.get_media_access(issue_number, media_id, actor)
            content = storage.get(access.object_key)
        except (CaseDeskError, OSError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return Response(content=content, media_type=access.mime_type)

    return app


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


def _payload_bool(
    payload: Mapping[str, Any], key: str, *, default: bool | None = None
) -> bool | None:
    if key not in payload:
        return default
    value = payload[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValidationError(f"字段 {key} 必须是布尔值")


def _payload_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValidationError(f"字段 {key} 必须是文本")
    return value


def _private_intake_requested(request: Request) -> bool:
    return request.query_params.get("private_intake") == "1"


def _private_intake_token(request: Request) -> str:
    token = request.headers.get(_PRIVATE_INTAKE_HEADER, "").strip()
    if not token or len(token) > 64:
        raise ValidationError("私聊交接令牌无效")
    return token


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














def _team_kind_label(kind: TeamKind) -> str:
    labels = {
        TeamKind.CONSULT_QUEUE: "咨询团队",
        TeamKind.DEV: "研发团队",
    }
    return labels.get(kind, kind.value)




def _role_label(role: MembershipRole) -> str:
    return "团队管理员" if role is MembershipRole.ADMIN else "成员"


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






async def _urlencoded_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


async def _json_object(request: Request) -> Mapping[str, Any]:
    try:
        body = await request.json()
    except Exception as error:
        raise ValidationError("请求必须是 JSON 对象") from error
    if not isinstance(body, dict):
        raise ValidationError("请求必须是 JSON 对象")
    return body


def _required(form: Mapping[str, str], key: str) -> str:
    value = form.get(key, "").strip()
    if not value:
        raise ValidationError(f"缺少字段：{key}")
    return value


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


_FORBIDDEN_RETURN_MARKER = "_kefu_forbidden_return"


def _browser_forbidden_response(
    request: Request,
    detail: str,
    *,
    fallback_path: str,
    mount_path: str,
    headers: Mapping[str, str] | None = None,
) -> HTMLResponse:
    target = _same_origin_referer_path(request, mount_path=mount_path)
    current_path = _unmount_path(request.url.path, mount_path=mount_path)
    if target is None:
        target = fallback_path

    target_path = urlsplit(target).path
    should_return = (
        request.query_params.get(_FORBIDDEN_RETURN_MARKER) != "1"
        and _unmount_path(target_path, mount_path=mount_path) != current_path
    )
    if should_return:
        target = _add_forbidden_return_marker(target)

    response_headers = {"Cache-Control": "no-store"}
    response_headers.update(headers or {})
    page = _static_page_content("forbidden.html")
    page = page.replace("__KEFU_BASE_PATH__", mount_path)
    page = page.replace("__KEFU_FORBIDDEN_MESSAGE__", escape(detail, quote=True))
    page = page.replace("__KEFU_FORBIDDEN_TARGET__", escape(target, quote=True))
    page = page.replace("__KEFU_FORBIDDEN_RETURN__", "true" if should_return else "false")
    page = page.replace("__KEFU_ISSUE_LIST__", escape(fallback_path, quote=True))
    return HTMLResponse(
        content=page,
        status_code=403,
        headers=response_headers,
    )


def _same_origin_referer_path(request: Request, *, mount_path: str) -> str | None:
    referer = request.headers.get("referer")
    if not referer:
        return None
    try:
        candidate = urlsplit(referer)
        current = urlsplit(str(request.url))
    except ValueError:
        return None
    if (
        candidate.scheme.lower() not in {"http", "https"}
        or candidate.scheme.lower() != current.scheme.lower()
        or candidate.netloc.lower() != current.netloc.lower()
        or candidate.username is not None
        or candidate.password is not None
        or not candidate.path.startswith("/")
        or candidate.path.startswith("//")
        or "\\" in candidate.path
    ):
        return None

    candidate_path = _unmount_path(candidate.path, mount_path=mount_path)
    current_path = _unmount_path(request.url.path, mount_path=mount_path)
    if candidate_path == current_path:
        return None
    return urlunsplit(("", "", candidate.path, candidate.query, ""))


def _unmount_path(path: str, *, mount_path: str) -> str:
    normalized_mount = mount_path.rstrip("/")
    if normalized_mount and (path == normalized_mount or path.startswith(f"{normalized_mount}/")):
        return path[len(normalized_mount) :] or "/"
    return path or "/"


def _add_forbidden_return_marker(path: str) -> str:
    parsed = urlsplit(path)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != _FORBIDDEN_RETURN_MARKER
    ]
    query.append((_FORBIDDEN_RETURN_MARKER, "1"))
    return urlunsplit(("", "", parsed.path, urlencode(query), ""))














def _datetime_local_value(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%dT%H:%M")






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


def _half_hour_hours_to_minutes(value: str, label: str) -> int:
    try:
        hours = Decimal(value)
        if not hours.is_finite():
            raise ValidationError(f"{label}必须是有效数字")
        minutes = hours * 60
        if (
            minutes < MIN_DEADLINE_INCREMENT_MINUTES
            or minutes > MAX_DEADLINE_INCREMENT_MINUTES
            or minutes % MIN_DEADLINE_INCREMENT_MINUTES != 0
        ):
            raise ValidationError(f"{label}必须在30分钟到可设置上限之间，并按30分钟递增")
        return int(minutes)
    except (DecimalException, ValueError, OverflowError) as error:
        raise ValidationError(f"{label}无效") from error






def _case_status_label(status: CaseStatus) -> str:
    return {
        CaseStatus.PENDING_CONFIRMATION: "待确定",
        CaseStatus.IN_PROGRESS: "处理中",
        CaseStatus.WAITING_CUSTOMER: "待客户反馈",
        CaseStatus.CLOSED: "关闭",
        CaseStatus.SUSPENDED: "挂起",
    }[status]




















def _priority_label_value(value: str) -> str:
    return {
        CasePriority.NORMAL.value: "普通",
        CasePriority.URGENT.value: "紧急",
        CasePriority.SEVERE.value: "严重",
    }.get(value, value)
































def _summary_json(summary: Any) -> dict[str, Any]:
    return {
        "issue_number": summary.issue_number,
        "bug_number": summary.bug_number,
        "title": summary.title,
        "customer_name": summary.customer_name,
        "issue_current_status": summary.issue_current_status,
        "workflow_status": summary.status.value,
        "can_accept": summary.can_accept,
        "priority": summary.priority.value,
        "status": summary.issue_current_status,
        "lifecycle_status": summary.lifecycle_status.value,
        "waiting_on": summary.waiting_on.value,
        "deadline": summary.deadline.isoformat(),
        "approaching_window_minutes": summary.approaching_window_minutes,
        "deadline_status": summary.deadline_status.value if summary.deadline_status else None,
        "updated_at": summary.updated_at.isoformat(),
        "version": summary.version,
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
        and (
            case.status is not CaseStatus.PENDING_CONFIRMATION
            or case.can_accept_pending
        )
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
        "waiting_on": case.waiting_on.value,
        "deadline": case.deadline.isoformat(),
        "approaching_window_minutes": case.approaching_window_minutes,
        "deadline_status": case.deadline_status.value if case.deadline_status else None,
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
        "consult_queue_name": case.consult_queue_name,
        "consult_handler_name": case.consult_handler_name,
        "current_consultant_name": case.current_consultant_name,
        "current_dev_team_id": str(case.current_dev_team_id),
        "current_dev_team_name": case.current_dev_team_name,
        "current_dev_team_lead_display_name": case.current_dev_team_lead_display_name,
        "can_transfer": case.can_transfer,
        "transferable_consultants": [
            {"id": str(user.id), "display_name": user.display_name}
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
                "metadata": dict(entry.metadata),
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


app = create_app()
