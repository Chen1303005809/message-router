# ruff: noqa: E501

"""Server-rendered H5 issue workspace with no business rules of its own."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException
from hmac import compare_digest
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from kefu.admin.service import (
    Administration,
    AdminOverview,
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
    EntryView,
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
    CaseEntryKind,
    CasePriority,
    CaseStatus,
    DeadlineStatus,
    DeliveryDestination,
    DeliveryStatus,
    EntrySide,
    IssueCurrentStatus,
    IssueModule,
    LifecycleStatus,
    MembershipRole,
    MessageIntent,
    PartKind,
    StoredMedia,
    TeamKind,
    User,
    WaitingOn,
)
from kefu.relay.handler import Relay
from kefu.web.auth import AuthenticationError, WebAuthenticator
from kefu.wecom.customer_groups import WeComCustomerGroupClient, WeComCustomerGroupError
from kefu.wecom.transport import InboundImagePart

configure_logging("WEB")
# httpx's built-in INFO log prints full request URLs, including access_token query values.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


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
    administration = Administration(session_factory)
    authenticator = authenticator or WebAuthenticator(session_factory, settings)
    storage = storage or _storage_from_settings(settings)
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
        token_hint = (
            "请输入部署时配置的初始化令牌。"
            if settings.admin_bootstrap_token
            else (
                "开发模式可使用与表单一致的 X-WeCom-UserId 请求头；"
                "生产环境请先配置 ADMIN_BOOTSTRAP_TOKEN。"
            )
        )
        body = f"""
        <main class="setup-card">
          <div class="eyebrow">首次使用 · H5 问题管理中心</div>
          <h1>初始化总管理员</h1>
          <p class="muted">总管理员使用企业微信 userid 登录，不单独设置密码。初始化完成后，人员授权、团队配置和研发/咨询群绑定都可以在管理中心可视化完成。</p>
          <div class="notice">{escape(token_hint)}</div>
          <form method="post" action="{external_path("/setup")}">
            <label>企业微信 userid
              <input name="wecom_userid" required maxlength="128" placeholder="例如 zhangsan">
            </label>
            <label>显示名称
              <input name="display_name" required maxlength="256" value="问题中心总管理员">
            </label>
            <label>初始化令牌
              <input name="token" type="password" autocomplete="off" placeholder="生产环境必填">
            </label>
            <button class="primary" type="submit">完成初始化并进入管理中心</button>
          </form>
        </main>
        """
        return HTMLResponse(_page("初始化总管理员", body, body_class="setup-page"))

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

    @app.get("/issues/new", response_class=HTMLResponse)
    async def new_issue_intake_page(request: Request) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            queues = issue_management.list_consult_queues(actor)
            dev_teams = issue_management.list_dev_teams(actor)
            issue_management.cleanup_expired_drafts()
        except CaseDeskError as error:
            raise _http_error(error) from error
        if not queues:
            raise _http_error(Forbidden("你没有可用的咨询队列权限，或咨询队列尚未绑定企微群聊"))
        queue_options = "".join(
            f'<option value="{queue.id}" '
            f"{'selected' if len(queues) == 1 else ''}>{escape(queue.name)}</option>"
            for queue in queues
        )
        if len(queues) > 1:
            queue_options = (
                '<option value="" disabled selected>选择队列</option>'
                + queue_options
            )
            queue_control = (
                '<label class="intake-queue"><span class="visually-hidden">咨询队列</span>'
                f'<select name="consult_queue_id" required aria-label="咨询队列">{queue_options}</select>'
                "</label>"
            )
        else:
            queue_control = f'<input type="hidden" name="consult_queue_id" value="{queues[0].id}">'
        if not dev_teams:
            raise _http_error(Forbidden("当前没有已绑定群聊的初次受理组"))
        group_context_scripts = (
            _wecom_group_context_scripts(external_path("/api/wecom/agent-js-sdk-config"))
            if wecom_customer_groups.is_configured
            else ""
        )
        body = f"""
        <main class="create-main intake-page">
          <header class="page-heading intake-heading">
            <h1>新建问题</h1>
            <a class="intake-back-link" href="{external_path("/issues")}">返回问题列表</a>
          </header>
          <form class="panel create-form intake-form" method="post" action="{external_path("/issues/draft")}" enctype="multipart/form-data">
            <textarea name="pasted_text" required maxlength="50000" rows="8" autofocus
              aria-label="粘贴问题内容，也可将图片拖入此处" data-image-drop-zone="true"
              placeholder="粘贴问题内容；图片可拖入此处…"></textarea>
            <input type="hidden" name="source_chat_id" value="">
            <input type="hidden" name="source_chat_status" value="not_reported">
            <input type="hidden" name="source_chat_error" value="">
            <div class="intake-actions">
              {_image_upload_controls(compact=True)}
              {queue_control}
              <button class="primary" type="submit" data-issue-draft-submit="true">上传</button>
            </div>
            <p class="draft-submit-status muted" data-issue-draft-status="true" role="status"
              aria-live="polite" hidden>
              <span class="draft-loading-spinner" aria-hidden="true"></span>
              正在整理…
            </p>
          </form>
          <script>
          (() => {{
            const form = document.querySelector('.intake-page form');
            const button = form?.querySelector('[data-issue-draft-submit="true"]');
            const status = form?.querySelector('[data-issue-draft-status="true"]');
            const sourceChatId = form?.querySelector('[name="source_chat_id"]');
            const sourceChatStatus = form?.querySelector('[name="source_chat_status"]');
            const sourceChatError = form?.querySelector('[name="source_chat_error"]');
            if (!form || !button || !status || !sourceChatId || !sourceChatStatus || !sourceChatError) return;
            let submitting = false;
            form.addEventListener('submit', async (event) => {{
              if (submitting) {{
                event.preventDefault();
                return;
              }}
              event.preventDefault();
              submitting = true;
              button.disabled = true;
              button.textContent = '正在上传…';
              status.hidden = false;
              form.setAttribute('aria-busy', 'true');
              try {{
                sourceChatId.value = await getWeComCustomerGroupId();
                sourceChatStatus.value = window.wecomCustomerGroupLookupStatus || 'unknown';
                sourceChatError.value = window.wecomCustomerGroupLookupError || 'sdk_error_field_empty';
              }} catch (_) {{
                sourceChatId.value = '';
                sourceChatStatus.value = 'client_exception';
                sourceChatError.value = 'client_exception';
              }}
              HTMLFormElement.prototype.submit.call(form);
            }});
          }})();
          </script>
          {group_context_scripts}
        </main>
        """
        return HTMLResponse(_page("上传问题材料", body, body_class="issue-intake-page"))

    @app.post("/issues/draft", include_in_schema=False)
    async def create_issue_draft(request: Request) -> RedirectResponse:
        request_id = getattr(request.state, "request_id", "untracked")
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await request.form()
        draft_processing_task_id: UUID | None = None
        try:
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
            source_chat_id = _optional_form_text(form, "source_chat_id")
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
                "issue_draft_customer_name_result request_id=%s group_name_present=%s customer_name=%r",
                request_id,
                bool(source_group_name),
                generated.get("customer_name", ""),
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
                issue_title=generated.get("issue_title", ""),
                issue_description=generated_description,
                initial_acceptance_team_id=selected_team.id if selected_team else None,
                suspected_type=generated.get("suspected_type", ""),
                current_situation=generated.get("current_situation", ""),
                issue_current_status="问题未回复",
                issue_source="工作台",
                deadline=datetime.now(ZoneInfo("Asia/Shanghai"))
                + timedelta(hours=settings.issue_default_expiry_hours),
                customer_name=generated.get("customer_name", ""),
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
        return RedirectResponse(
            external_path(f"/issues/drafts/{draft.id}"),
            status_code=303,
        )

    @app.get("/issues/drafts/{draft_id}", response_class=HTMLResponse)
    async def issue_draft_review(request: Request, draft_id: UUID) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            draft = issue_management.get_intake_draft(draft_id, actor)
            queues = issue_management.list_consult_queues(actor)
            dev_teams = issue_management.list_dev_teams(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        team_options = '<option value="">请选择初次受理组</option>' + "".join(
            f'<option value="{team.id}" '
            f"{'selected' if team.id == draft.fields.initial_acceptance_team_id else ''}>"
            f"{escape(team.name)}</option>"
            for team in dev_teams
        )
        queue_name = next(
            (queue.name for queue in queues if queue.id == draft.consult_queue_id),
            "已登记咨询队列",
        )
        parts_html = "".join(
            (
                f'<pre class="intake-part">{escape(part.text)}</pre>'
                if isinstance(part, TextPart)
                else (
                    f'<img class="intake-image" src="{external_path(f"/issues/drafts/{draft.id}/media/{part.media_id}")}" '
                    f'alt="问题图片附件">'
                )
            )
            for part in draft.parts
        )
        initial_deadline = _datetime_local_value(draft.initial_expires_at)
        fields = draft.fields
        module_options = _issue_module_options(fields.suspected_type)
        body = f"""
        <main class="create-main intake-review-page">
          <a class="back-link" href="{external_path("/issues/new")}">← 重新上传材料</a>
          <form class="panel create-form" method="post" action="{external_path(f"/issues/drafts/{draft.id}/confirm")}">
            <label>问题标题 <input name="issue_title" required maxlength="512" value="{escape(fields.issue_title)}" placeholder="Agent 生成的简短问题概括"></label>
            <label>转发正文 <textarea name="issue_description" required maxlength="20000" rows="8">{escape(fields.issue_description)}</textarea></label>
            <p class="muted intake-review-hint">标题和正文会转发给初次受理组，图片按原顺序作为附件转发。</p>
            <div class="intake-key-fields">
              <label>客户名称 <input name="customer_name" required maxlength="256" value="{escape(fields.customer_name)}" placeholder="请输入客户名称"></label>
              <div class="intake-readonly-field"><span>客户群聊名称</span><strong>{escape(draft.source_group_name or "未获取")}</strong></div>
              <label>初次受理组 <select name="initial_acceptance_team_id" required>{team_options}</select></label>
              <label>怀疑类型 <select name="suspected_type" required>{module_options}</select></label>
            </div>
            <details class="panel intake-source">
              <summary>收到的材料</summary>
              <div class="intake-source-content"><p class="muted">咨询队列：{escape(queue_name)}</p>{parts_html}</div>
            </details>
            <input type="hidden" name="customer_contact_name" value="{escape(fields.customer_contact_name)}">
            <input type="hidden" name="customer_contact_method" value="{escape(fields.customer_contact_method)}">
            <input type="hidden" name="issue_current_status" value="{escape(fields.issue_current_status)}">
            <input type="hidden" name="current_situation" value="{escape(fields.current_situation)}">
            <input type="hidden" name="deadline" value="{initial_deadline}">
            <button class="primary" type="submit">确认并推送给初次受理组</button>
          </form>
        </main>
        """
        return HTMLResponse(_page("确认问题", body, body_class="issue-review-page"))

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

    @app.post("/issues/drafts/{draft_id}/confirm", include_in_schema=False)
    async def confirm_issue_draft(request: Request, draft_id: UUID) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await request.form()
        try:
            fields = IssueFields(
                issue_title=_required_form_text(form, "issue_title"),
                issue_description=_required_form_text(form, "issue_description"),
                customer_name=_required_form_text(form, "customer_name"),
                customer_contact_name=_optional_form_text(form, "customer_contact_name") or "",
                customer_contact_method=_optional_form_text(form, "customer_contact_method") or "",
                initial_acceptance_team_id=UUID(
                    _required_form_text(form, "initial_acceptance_team_id")
                ),
                suspected_type=_optional_form_text(form, "suspected_type") or "",
                issue_current_status=_required_form_text(form, "issue_current_status"),
                current_situation=_optional_form_text(form, "current_situation") or "",
                issue_source=_optional_form_text(form, "issue_source") or "工作台",
                deadline=_parse_local_deadline(_required_form_text(form, "deadline")),
            )
            result = issue_management.confirm_intake(draft_id, fields, actor)
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        finally:
            await form.close()
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
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            overview = administration.overview(actor)
            users = administration.list_users(actor, query=q)
            all_users = users if not q.strip() else administration.list_users(actor)
            teams = administration.list_teams(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        body = _admin_dashboard(
            actor=actor,
            overview=overview,
            users=users,
            member_options=all_users,
            teams=teams,
            query=q,
            path_for=external_path,
        )
        return HTMLResponse(_page("组织与授权", body, body_class="admin-page"))

    @app.get("/api/admin/overview")
    async def admin_overview_api(request: Request) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            return JSONResponse(_admin_overview_json(administration.overview(actor)))
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

    @app.get("/issues", response_class=HTMLResponse)
    async def events_page(request: Request, state: str = "open", priority: str = "") -> Response:
        if state not in {"mine", "dev", "open", "closed"}:
            state = "open"
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            overview = issue_management.overview(actor)
            cases = issue_management.list(_case_filter_for_state(state, priority), actor)
            consult_queues = issue_management.list_consult_queues(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        cards = "".join(_case_card(summary, external_path) for summary in cases.items)
        cards = cards or '<p class="empty">当前筛选下没有问题。</p>'
        events_path = external_path("/issues")
        tabs = " ".join(
            f'<a class="{"active" if state == value else ""}" '
            f'href="{_event_filter_url(events_path, value, priority)}">{label}</a>'
            for value, label in (
                ("open", "问题列表"),
                ("closed", "已关闭"),
            )
        )
        selected_priority = CasePriority(priority) if priority else None
        priority_filters = " ".join(
            f'<a class="{"active" if selected_priority is level else ""}" '
            f'href="{_event_filter_url(events_path, state, level.value if level else "")}">{label}</a>'
            for level, label in (
                (None, "全部等级"),
                (CasePriority.NORMAL, "普通"),
                (CasePriority.URGENT, "紧急"),
                (CasePriority.SEVERE, "严重"),
            )
        )
        admin_link = (
            f'<a class="nav-admin" href="{external_path("/admin")}">组织与授权</a>'
            if administration.is_global_admin(actor)
            else ""
        )
        queue_link = f'<a href="{external_path("/status-queue")}">异步任务队列</a>'
        create_link = (
            f'<a class="nav-create" href="{external_path("/issues/new")}">＋ 新建问题</a>'
            if consult_queues
            else ""
        )
        
        return HTMLResponse(
            _page(
                "问题中心",
                f'<header class="topbar"><div><div class="eyebrow"></div>'
                f"<nav>{tabs}{queue_link}{admin_link}{create_link}</nav></header>"
                f'<section class="filter-panel"><div class="filter-label">紧急程度</div>'
                f'<nav class="priority-filters">{priority_filters}</nav></section>'
                f'<main class="event-list">{cards}</main>',
                body_class="event-page",
            )
        )

    @app.get("/api/issues")
    async def issues_api(request: Request, state: str = "open", priority: str = "") -> JSONResponse:
        actor = actor_or_error(request)
        try:
            page = issue_management.list(_case_filter_for_state(state, priority), actor)
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
        active_tasks = desk.list_issue_status_queue()
        active_draft_tasks = desk.list_issue_draft_processing_tasks()
        recent_changes = desk.list_recent_issue_status_changes()
        task_cards = "".join(
            _issue_draft_queue_card(item)
            for item in active_draft_tasks
        ) + "".join(
            _status_queue_card(item, external_path) for item in active_tasks
        ) or '<p class="empty">当前没有排队或执行中的任务。</p>'
        task_count = len(active_tasks) + len(active_draft_tasks)
        change_cards = "".join(
            _recent_status_change_card(item, external_path) for item in recent_changes
        ) or '<p class="empty">暂无状态变化记录。</p>'
        events_path = external_path("/issues")
        queue_path = external_path("/status-queue")
        body = f"""
        <main class="status-queue-main">
          <header class="topbar"><div><div class="eyebrow">问题工作台 · 后台任务</div>
            <h1>异步任务队列</h1>
            <p class="muted">这里显示 Agent 草稿整理和问题状态判定任务；页面每 5 秒自动刷新。</p></div>
            <nav><a href="{queue_path}">立即刷新</a><a href="{events_path}">问题工作台</a></nav>
          </header>
          <section class="queue-section panel">
            <div class="section-heading"><h2>当前任务</h2><span>显示 {task_count} 条，最多各 100 条</span></div>
            <div class="queue-stack">{task_cards}</div>
          </section>
          <section class="queue-section panel">
            <div class="section-heading"><h2>最近状态变化</h2><span>最多 50 条</span></div>
            <div class="status-history">{change_cards}</div>
          </section>
        </main>
        <script>window.setTimeout(() => window.location.reload(), 5000);</script>
        """
        return HTMLResponse(_page("异步任务队列", body, body_class="status-queue-page"))

    @app.get("/api/status-queue")
    async def status_queue_api(request: Request) -> JSONResponse:
        actor_or_error(request)
        active_tasks = desk.list_issue_status_queue()
        active_draft_tasks = desk.list_issue_draft_processing_tasks()
        recent_changes = desk.list_recent_issue_status_changes()
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
            }
        )

    @app.get("/legacy/events/new", response_class=HTMLResponse, include_in_schema=False)
    async def new_case_form_page(request: Request) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            queues = issue_management.list_consult_queues(actor)
            dev_teams = issue_management.list_dev_teams(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        if not queues:
            raise _http_error(Forbidden("你没有可用的咨询队列权限，或所选咨询队列尚未绑定咨询群"))
        queue_options = "".join(
            f'<option value="{queue.id}">{escape(queue.name)}</option>' for queue in queues
        )
        dev_team_options = "".join(
            f'<option value="{team.id}">{escape(team.name)}'
            f"（经办人：{escape(team.lead_display_name or team.name)}）</option>"
            for team in dev_teams
        )
        issue_module_options = _issue_module_options("")
        issue_status_options = _issue_status_options("问题未回复")
        body = f"""
        <main class="create-main">
          <a class="back-link" href="{external_path("/issues")}">← 返回问题中心</a>
          <header class="page-heading"><div class="eyebrow">H5 问题中心</div>
            <h1>创建新问题</h1><p class="muted">填写问题和客户信息，补充图片后直接交给研发跟进。</p></header>
          <form class="panel create-form" method="post" action="{external_path("/legacy/events/new")}" enctype="multipart/form-data">
            <h2>问题信息</h2>
            <label>问题标题 <input name="title" required maxlength="512" placeholder="例如：订单提交后页面报错"></label>
            <label>咨询经办显示名 <input name="consult_handler_name" required maxlength="256" placeholder="可填写姓名或团队内使用的称呼"></label>
            <label>问题描述 <textarea name="description" required maxlength="20000" rows="6" placeholder="描述客户现象、发生时间、影响范围和复现步骤"></textarea></label>
            <div class="form-row">
              <label>怀疑类型（问题所属模块） <select name="suspected_type" required>{issue_module_options}</select></label>
              <label>问题状态 <select name="issue_current_status" required>{issue_status_options}</select></label>
            </div>
            {_image_upload_controls()}
            <label>客户 / 企业名称 <input name="customer_name" required maxlength="256" placeholder="例如：杭州某某科技"></label>
            <label>客户群聊名称（选填） <input name="customer_group_name" maxlength="256" placeholder="客户所在企业微信群聊名称"></label>
            <div class="form-row">
              <label>联系人（选填） <input name="customer_contact_name" maxlength="256"></label>
              <label>联系方式（选填） <input name="customer_contact_method" maxlength="256" placeholder="电话、邮箱或其他联系方法"></label>
            </div>
            <div class="form-row">
              <label>紧急程度
                <select name="priority">
                  <option value="normal" selected>普通</option><option value="urgent">紧急</option>
                  <option value="severe">严重</option>
                </select>
              </label>
              <label>咨询团队 <select name="consult_queue_id" required>
                <option value="" disabled selected>选择咨询团队</option>{queue_options}
              </select></label>
            </div>
            <label>研发处理团队 <select name="dev_team_id" required>
              <option value="" disabled selected>选择研发处理团队</option>{dev_team_options}
            </select></label>
            <label>截止时间（选填）
              <input name="deadline" type="datetime-local" step="60">
            </label>
            <p class="muted">咨询经办显示名由本问题填写，独立于登录账号；研发经办显示名沿用团队设置。群聊记录不按发送成员 userid 归属。</p>
            <p class="muted">描述文字必填，图片可选。截止时间按中国标准时间填写；留空时默认为创建后 3 小时。临近期限提醒默认提前 24 小时，创建后也可在详情中调整。</p>
            <button class="primary" type="submit">创建并发送给研发</button>
          </form>
        </main>
        """
        return HTMLResponse(_page("创建问题", body, body_class="case-create-page"))

    @app.post("/legacy/events/new", include_in_schema=False)
    async def create_case_from_h5(request: Request) -> RedirectResponse:
        actor = page_actor_or_login(request)
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
                case = issue_management.get(issue_number, actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        can_use_side_selector = administration.is_global_admin(actor)
        deliveries_by_entry: dict[UUID, list[Any]] = {}
        for delivery in case.deliveries:
            deliveries_by_entry.setdefault(delivery.entry_id, []).append(delivery)
        entries = "".join(
            _render_entry(
                case.issue_number,
                entry,
                external_path,
                deliveries_by_entry.get(entry.id, ()),
            )
            for entry in case.entries
        )
        events_path = external_path("/issues")
        message_form = _message_form(
            case.issue_number,
            case.version,
            case.lifecycle_status,
            external_path,
            show_side=can_use_side_selector,
        )
        lifecycle_forms = _lifecycle_forms(case, external_path)
        top_close_control = _top_close_control(case, external_path)
        action_panel = (
            f'<section class="action-panel panel">{message_form}{lifecycle_forms}</section>'
            if message_form or lifecycle_forms
            else ""
        )
        body = f"""
        <main class="case-main">
          <a class="back-link" href="{events_path}">← 返回问题工作台</a>
          {top_close_control}
          <header class="case-titlebar">
            <div><h1>{escape(case.title)}</h1></div>
            <div class="case-state-badges">
              {_deadline_badge(case.deadline_status)}
              {_priority_badge(case.priority)}
            </div>
          </header>
          <section class="issue-fields panel">
            <h2>问题信息</h2>
            <div class="issue-fields-grid">
              <div class="issue-field issue-field-wide"><span>问题描述</span><p>{escape(case.issue_description or case.title)}</p></div>
              <div class="issue-field"><span>初次受理组</span><strong>{escape(case.initial_acceptance_team_name or case.current_dev_team_name)}</strong></div>
              <div class="issue-field"><span>怀疑类型（问题所属模块）</span><strong>{escape(case.suspected_type or "未填写")}</strong></div>
              <div class="issue-field"><span>现状说明</span><p>{escape(case.current_situation or "未填写")}</p></div>
              <div class="issue-field"><span>问题状态</span>{_issue_current_status_control(case, external_path)}</div>
              <div class="issue-field"><span>问题初次过期时间</span><strong>{_format_datetime(case.initial_expires_at or case.deadline)}</strong></div>
              <div class="issue-field"><span>BUG 编号</span><strong>{escape(case.bug_number or "不适用")}</strong></div>
              <div class="issue-field"><span>首次记录人</span><strong>{escape(case.first_recorder_name)}</strong></div>
            </div>
          </section>
          <section class="case-overview panel">
            <div class="case-overview-heading"><h2>客户与责任信息</h2>
              <div class="updated-at">最近更新 {_format_datetime(case.updated_at)}</div></div>
            <div class="case-meta-grid">
              <div><span>客户 / 企业</span><strong>{escape(case.customer_name or "未录入")}</strong></div>
              <div><span>客户群聊名称</span><strong>{escape(case.customer_group_name or "未获取")}</strong></div>
              <div><span>咨询团队</span><strong>{escape(case.consult_queue_name)}</strong></div>
              <div><span>创建时间</span><strong>{_format_datetime(case.created_at)}</strong></div>
              <div><span>截止时间</span><strong>{_format_datetime(case.deadline)}</strong></div>
              <div><span>临近期限提醒</span><strong>提前 {_duration_label(case.approaching_window_minutes)}</strong></div>
            </div>
          </section>
          {_metadata_form(case, external_path)}
          {_transfer_controls(case, external_path)}
          {_deadline_controls(case, external_path)}
          <section class="timeline-section">
            <div class="section-heading"><h2>问题时间线</h2><span>{len(case.entries)} 条记录</span></div>
            <div class="timeline">{entries}</div>
          </section>
          {action_panel}
        </main>
        """
        return HTMLResponse(_page(case.title, body, body_class="case-detail-page"))

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
        "members": [_managed_team_member_json(member) for member in team.members],
    }


def _admin_dashboard(
    *,
    actor: Actor,
    overview: AdminOverview,
    users: tuple[ManagedUser, ...],
    member_options: tuple[ManagedUser, ...],
    teams: tuple[ManagedTeam, ...],
    query: str,
    path_for: Callable[[str], str],
) -> str:
    admin_path = path_for("/admin")
    events_path = path_for("/issues")
    visible_users = tuple(user for user in member_options if user.active)
    initialized_label = (
        f"{overview.initialized_dev_team_count}/{overview.dev_team_count} 个研发群已绑定"
        if overview.dev_team_count
        else "暂未创建研发团队"
    )
    search_value = escape(query)

    user_rows = (
        "".join(_admin_user_row(user, path_for=path_for) for user in users)
        or '<p class="empty">没有匹配的成员。</p>'
    )
    team_cards = (
        "".join(_admin_team_card(team, users=visible_users, path_for=path_for) for team in teams)
        or '<p class="empty">还没有团队，请先创建咨询团队或研发团队。</p>'
    )
    return f"""
    <header class="topbar admin-topbar">
      <div>
        <div class="eyebrow">H5 问题管理中心</div>
        <h1>组织与授权</h1>
        <p class="muted">总管理员：{escape(actor.user_id.hex[:8])} · 管理成员、团队与群聊路由</p>
      </div>
      <nav><a href="{events_path}">返回问题中心</a><a class="active" href="{admin_path}">管理设置</a></nav>
    </header>
    <main class="admin-main">
      <section class="hero-banner">
        <div><span class="status-dot"></span><strong>权限配置已启用</strong>
          <p>先录入企业微信成员，再将成员加入咨询团队或研发团队；角色为“管理员”的成员可以处理本团队的交接和群绑定。</p>
        </div>
        <span class="badge badge-blue">总管理员</span>
      </section>

      <section class="stats-grid">
        <div class="stat-card"><span>在岗成员</span><strong>{overview.active_user_count}</strong><small>共 {overview.user_count} 个身份</small></div>
        <div class="stat-card"><span>咨询团队</span><strong>{overview.consult_queue_count}</strong><small>按团队隔离可见范围</small></div>
        <div class="stat-card"><span>研发团队</span><strong>{overview.dev_team_count}</strong><small>{escape(initialized_label)}</small></div>
        <div class="stat-card"><span>总管理员</span><strong>{overview.global_admin_count}</strong><small>至少保留 1 名在岗管理员</small></div>
      </section>

      <section class="admin-grid">
        <div class="panel" id="users">
          <div class="panel-heading"><div><div class="eyebrow">Step 1</div><h2>成员与总权限</h2></div><span class="count-pill">{overview.user_count}</span></div>
          <p class="panel-intro">企业微信 userid 用于 H5 登录和账号级权限。群聊绑定指令不校验发送人身份；群内正式回复按绑定侧别和问题标记记录。咨询经办显示名在创建问题时单独填写；研发显示名沿用团队负责人展示名。</p>
          <form class="search-form" method="get" action="{admin_path}">
            <input name="q" value="{search_value}" placeholder="搜索姓名或 userid">
            <button type="submit">搜索</button>
            {f'<a class="button-link" href="{admin_path}">清除</a>' if query else ""}
          </form>
          <details class="add-box" open>
            <summary>＋ 新增企业成员</summary>
            <form method="post" action="{path_for("/admin/users")}" class="form-grid">
              <label>显示名称<input name="display_name" required maxlength="256" placeholder="例如 张三"></label>
              <label>企业微信 userid<input name="wecom_userid" required maxlength="128" placeholder="例如 zhangsan"></label>
              <label class="check-label"><input type="checkbox" name="is_global_admin"> 同时设为总管理员</label>
              <button class="primary" type="submit">添加成员</button>
            </form>
          </details>
          <div class="managed-list">{user_rows}</div>
        </div>

        <div class="panel" id="teams">
          <div class="panel-heading"><div><div class="eyebrow">Step 2</div><h2>团队与授权</h2></div><span class="count-pill">{overview.team_count}</span></div>
          <p class="panel-intro">咨询团队负责问题归属和咨询侧可见范围，并可绑定咨询群接收通知；研发团队负责问题路由和研发群消息转发，负责人姓名仅作展示，不关联成员账号。</p>
          <details class="add-box" open>
            <summary>＋ 新增团队</summary>
            <form method="post" action="{path_for("/admin/teams")}" class="form-grid team-create-form">
              <label>团队类型<select name="kind" data-team-kind-select="true"><option value="consult_queue">{_team_kind_label(TeamKind.CONSULT_QUEUE)}</option><option value="dev">{_team_kind_label(TeamKind.DEV)}</option></select></label>
              <label>团队名称<input name="name" required maxlength="256" placeholder="例如 平台研发组"></label>
              <label class="team-lead-display-name-field" hidden>研发负责人展示名<input name="lead_display_name" data-team-lead-input="true" maxlength="256" placeholder="仅显示，不关联企业微信成员" disabled></label>
              <button class="primary" type="submit">创建团队</button>
            </form>
            {_team_create_form_script()}
          </details>
          <div class="managed-list">{team_cards}</div>
        </div>
      </section>

      <section class="permission-note">
        <strong>授权规则</strong>
          <span>成员 = 可查看和处理团队问题；团队管理员 = 可在本团队内执行交接、转交和群聊绑定；总管理员 = 可管理全组织并查看全部问题。</span>
      </section>
    </main>
    """


def _team_create_form_script() -> str:
    return """
    <script>
    (() => {
      const form = document.querySelector('.team-create-form');
      const kind = form?.querySelector('[data-team-kind-select="true"]');
      const leadField = form?.querySelector('.team-lead-display-name-field');
      const leadInput = form?.querySelector('[data-team-lead-input="true"]');
      if (!kind || !leadField || !leadInput) return;

      const updateLeadField = () => {
        const isDevTeam = kind.value === "dev";
        leadField.hidden = !isDevTeam;
        leadInput.disabled = !isDevTeam;
      };

      kind.addEventListener("change", updateLeadField);
      updateLeadField();
    })();
    </script>
    """


def _admin_user_row(user: ManagedUser, *, path_for: Callable[[str], str]) -> str:
    user_id = escape(str(user.id))
    memberships = (
        "".join(
            f'<span class="tag {"tag-muted" if not membership.active else ""}">'
            f"{escape(_team_kind_label(membership.team_kind))} · {escape(membership.team_name)} · "
            f"{escape(_role_label(membership.role))}</span>"
            for membership in user.memberships
        )
        or '<span class="muted">尚未加入团队</span>'
    )
    global_badge = '<span class="badge badge-amber">总管理员</span>' if user.is_global_admin else ""
    active_badge = (
        '<span class="badge badge-green">在岗</span>'
        if user.active
        else '<span class="badge badge-gray">已停用</span>'
    )
    checked_active = " checked" if user.active else ""
    checked_admin = " checked" if user.is_global_admin else ""
    return f"""
    <details class="managed-row">
      <summary><span class="avatar">{escape(user.display_name[:1])}</span><span class="user-summary"><strong>{escape(user.display_name)}</strong><small>{escape(user.wecom_userid)}</small></span>{active_badge}{global_badge}<span class="chevron">⌄</span></summary>
      <div class="managed-detail">
        <div class="tag-list">{memberships}</div>
        <form method="post" action="{path_for(f"/admin/users/{user_id}")}" class="edit-form">
          <label>显示名称<input name="display_name" required maxlength="256" value="{escape(user.display_name)}"></label>
          <label class="check-label"><input type="checkbox" name="active"{checked_active}> 在岗，可登录</label>
          <label class="check-label"><input type="checkbox" name="is_global_admin"{checked_admin}> 总管理员</label>
          <button type="submit">保存成员权限</button>
        </form>
      </div>
    </details>
    """


def _admin_team_card(
    team: ManagedTeam,
    *,
    users: tuple[ManagedUser, ...],
    path_for: Callable[[str], str],
) -> str:
    team_id = escape(str(team.id))
    team_badge = _team_kind_label(team.kind)
    status = (
        '<span class="badge badge-green">启用</span>'
        if team.active
        else '<span class="badge badge-gray">已停用</span>'
    )
    checked = " checked" if team.active else ""
    member_rows = (
        "".join(_admin_team_member_row(team, member, path_for=path_for) for member in team.members)
        or '<p class="empty">还没有授权成员。</p>'
    )
    options = (
        "".join(
            f'<option value="{escape(str(user.id))}">{escape(user.display_name)}（{escape(user.wecom_userid)}）</option>'
            for user in users
        )
        or '<option value="">请先添加在岗成员</option>'
    )
    lead_editor = (
        f'<label>负责人展示名<input name="lead_display_name" maxlength="256" '
        f'value="{escape(team.lead_display_name or "")}" placeholder="仅显示，不关联成员账号"></label>'
        if team.kind is TeamKind.DEV
        else ""
    )
    channel = ""
    if team.kind in (TeamKind.DEV, TeamKind.CONSULT_QUEUE):
        chat_label = "研发群" if team.kind is TeamKind.DEV else "咨询群"
        channel_value = escape(team.channel_chatid or "")
        channel_status = (
            f'<span class="muted">当前群：{channel_value}</span>'
            if team.channel_initialized
            else f'<span class="warning-text">尚未绑定{chat_label}</span>'
        )
        channel = f"""
        <div class="channel-box"><div><strong>{chat_label}通道</strong>{channel_status}</div>
          <form method="post" action="{path_for(f"/admin/teams/{team_id}/channel")}" class="inline-form">
            <input name="chatid" required maxlength="256" value="{channel_value}" placeholder="粘贴企业微信群 chatid">
            <button type="submit">绑定</button>
          </form>
        </div>
        """
    return f"""
    <article class="team-card">
      <div class="team-header"><div><span class="eyebrow">{escape(team_badge)}</span><h3>{escape(team.name)}</h3></div>{status}</div>
      <form method="post" action="{path_for(f"/admin/teams/{team_id}")}" class="team-edit-form">
        <input name="name" required maxlength="256" value="{escape(team.name)}">
        {lead_editor}
        <label class="check-label"><input type="checkbox" name="active"{checked}> 启用</label>
        <button class="subtle-button" type="submit">保存</button>
      </form>
      {channel}
      <div class="member-heading"><strong>授权成员</strong><span>{sum(1 for member in team.members if member.active)} 人在岗</span></div>
      <div class="member-list">{member_rows}</div>
      <form method="post" action="{path_for(f"/admin/teams/{team_id}/members")}" class="add-member-form">
        <select name="user_id" required>{options}</select>
        <select name="role"><option value="member">成员</option><option value="admin">团队管理员</option></select>
        <button type="submit">授权加入</button>
      </form>
    </article>
    """


def _admin_team_member_row(
    team: ManagedTeam,
    member: ManagedTeamMember,
    *,
    path_for: Callable[[str], str],
) -> str:
    team_id = escape(str(team.id))
    user_id = escape(str(member.user_id))
    active = (
        '<span class="status-dot small"></span>'
        if member.active
        else '<span class="status-dot small off"></span>'
    )
    role = _role_label(member.role)
    return f"""
    <div class="member-row"><div>{active}<span><strong>{escape(member.display_name)}</strong><small>{escape(member.wecom_userid)} · {escape(role)}</small></span></div>
      <div class="member-actions">
        <form method="post" action="{path_for(f"/admin/teams/{team_id}/members")}" class="role-form">
          <input type="hidden" name="user_id" value="{user_id}">
          <select name="role"><option value="member"{" selected" if member.role is MembershipRole.MEMBER else ""}>成员</option><option value="admin"{" selected" if member.role is MembershipRole.ADMIN else ""}>管理员</option></select>
          <button class="subtle-button" type="submit">保存</button>
        </form>
        <form method="post" action="{path_for(f"/admin/teams/{team_id}/members/{user_id}/remove")}"><button class="danger-button" type="submit">移除</button></form>
      </div>
    </div>
    """


def _team_kind_label(kind: TeamKind) -> str:
    return "咨询团队" if kind is TeamKind.CONSULT_QUEUE else "研发团队"


def _role_label(role: MembershipRole) -> str:
    return "团队管理员" if role is MembershipRole.ADMIN else "成员"


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


def _storage_from_settings(settings: Settings) -> ObjectStorage:
    return object_storage_from_settings(settings)


def _case_filter_for_state(state: str, priority: str = "") -> CaseFilter:
    base = {
        "mine": CaseFilter(
            lifecycle_status=LifecycleStatus.OPEN,
            waiting_on=WaitingOn.CONSULT,
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
    )


def _event_filter_url(path: str, state: str, priority: str) -> str:
    values = {"state": state if state in {"mine", "dev", "open", "closed"} else "open"}
    if priority:
        values["priority"] = CasePriority(priority).value
    return f"{path}?{urlencode(values)}"


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


_WORKBENCH_CSS = """
:root {
  color-scheme: light;
  --ink: #18243a;
  --muted: #66758c;
  --line: #e0e7f0;
  --surface: #ffffff;
  --page: #f2f5f9;
  --blue: #285fe8;
  --blue-soft: #edf3ff;
  --green: #087a54;
  --amber: #9a5b00;
  --red: #b42318;
}
* { box-sizing: border-box; }
body {
  max-width: 1040px;
  padding: 24px;
  color: var(--ink);
  background: var(--page);
  line-height: 1.55;
}
body.event-page { max-width: 1180px; padding: 30px 28px 48px; }
body.case-detail-page, body.case-create-page { max-width: 980px; padding: 26px 24px 48px; }
body.status-queue-page { max-width: 1120px; padding: 30px 28px 48px; }
body.admin-page { max-width: 1200px; background: var(--page); }
body.setup-page { max-width: 100%; min-height: 100vh; padding: 24px; background: #edf3ff; }
h1 { color: var(--ink); }
h2 { color: #23344f; }
.muted, .case-card small { color: var(--muted); }
.topbar { align-items: center; margin-bottom: 22px; }
.topbar h1 { margin: 4px 0 0; font-size: 30px; }
.topbar nav { gap: 5px; }
.topbar nav a {
  padding: 8px 11px;
  border-radius: 9px;
  color: #52627b;
  font-size: 13px;
  font-weight: 650;
}
.topbar nav a.active, .topbar nav a:hover { color: var(--blue); background: var(--blue-soft); }
.topbar nav .nav-create { color: #fff; background: var(--blue); }
.topbar nav .nav-create:hover { color: #fff; background: #174bc4; }
.overview-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
.overview-card { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 16px; border: 1px solid var(--line); border-radius: 13px; background: var(--surface); box-shadow: 0 5px 18px #162a4608; }
.overview-card span { color: var(--muted); font-size: 13px; font-weight: 650; }
.overview-card strong { color: var(--ink); font-size: 24px; line-height: 1; font-variant-numeric: tabular-nums; }
.filter-panel, .panel, .case-card, .case-overview, .metadata-editor {
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
  box-shadow: 0 5px 18px #162a4608;
}
.filter-panel { padding: 13px 16px; margin-bottom: 16px; }
.filter-label { color: var(--muted); font-size: 12px; font-weight: 700; margin-bottom: 6px; }
.priority-filters { display: flex; gap: 6px; margin: 0; }
.priority-filters a { padding: 5px 9px; border-radius: 8px; color: #58667b; font-size: 13px; }
.priority-filters a.active, .priority-filters a:hover { color: var(--blue); background: var(--blue-soft); }
.event-list { max-width: none; display: grid; gap: 11px; }
.case-card {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 16px 18px;
  box-shadow: 0 3px 12px #162a4607;
  transition: border-color .15s ease, transform .15s ease, box-shadow .15s ease;
}
.case-card:hover { border-color: #b6c9f3; box-shadow: 0 8px 20px #162a4610; transform: translateY(-1px); }
.case-card-link { display: grid; gap: 8px; color: inherit; text-decoration: none; }
.case-card-link:focus-visible { outline: 3px solid #9db8ff; outline-offset: 3px; border-radius: 6px; }
.case-card-auto-accept { position: relative; }
.case-card-hitbox { position: absolute; inset: 0; z-index: 1; width: 100%; height: 100%; padding: 0; border: 0; border-radius: 14px; color: transparent; background: transparent; cursor: pointer; }
.case-card-hitbox:hover { border-color: transparent; background: transparent; }
.case-card-hitbox:focus-visible { outline: 3px solid #9db8ff; outline-offset: 3px; }
.case-card-content { display: grid; gap: 8px; pointer-events: none; }
.case-card-top, .case-card-meta, .case-card-badges { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.case-card-title { font-size: 16px; font-weight: 750; line-height: 1.4; }
.case-customer { font-size: 13px; color: #41516a; }
.case-card-meta { justify-content: space-between; font-size: 12px; }
.case-ref { font: 600 11px ui-monospace, SFMono-Regular, Menlo, monospace; color: #7c8aa0; }
.status-badge, .delivery-badge, .entry-intent {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 4px 9px; border-radius: 999px;
  font-size: 11px; font-weight: 750; white-space: nowrap;
}
.status-open { color: #075985; background: #e0f2fe; }
.status-closed { color: #58667b; background: #edf0f4; }
.case-status-pending { color: #805000; background: #fff2cc; }
.case-status-progress { color: #087a54; background: #e5f7ef; }
.case-status-feedback { color: #155eef; background: #eaf1ff; }
.case-status-suspended { color: #6941c6; background: #f2edff; }
.issue-current-status { color: #344054; background: #eef2f6; }
.deadline-approaching { color: #9a5b00; background: #fff2cc; }
.deadline-overdue { color: #b42318; background: #fee4e2; }
.priority-normal { color: #58667b; background: #edf0f4; }
.priority-urgent { color: #9a5b00; background: #fff2cc; }
.priority-severe { color: #b42318; background: #fee4e2; }
.back-link { display: inline-block; margin-bottom: 14px; color: #52627b; font-size: 13px; font-weight: 650; }
.back-link:hover { color: var(--blue); }
.page-heading { margin-bottom: 18px; }
.page-heading h1 { margin: 4px 0; }
.case-titlebar { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin: 2px 0 18px; }
.case-titlebar h1 { margin: 0; font-size: 27px; line-height: 1.25; }
.case-state-badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.top-close-control { margin: 0 0 12px; }
.top-close-control button.status-action-close { min-height: 40px; padding: 8px 15px; border: 1px solid transparent; border-radius: 9px; color: #fff; background: #b42318; font-size: 13px; font-weight: 750; }
.top-close-control button.status-action-close:hover:not(:disabled) { background: #8f1c13; }
.case-overview { padding: 18px 20px; }
.case-overview-heading, .section-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.case-overview-heading h2, .section-heading h2 { margin-bottom: 13px; font-size: 17px; }
.updated-at, .section-heading span { color: var(--muted); font-size: 12px; }
.case-meta-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 15px 20px; }
.case-meta-grid div { min-width: 0; }
.case-meta-grid span { display: block; color: var(--muted); font-size: 11px; margin-bottom: 3px; }
.case-meta-grid strong { display: block; overflow-wrap: anywhere; font-size: 13px; }
.transfer-controls, .deadline-controls { margin-top: 12px; padding: 18px 20px; }
.transfer-controls h2, .deadline-controls h2 { margin: 0 0 5px; font-size: 16px; }
.transfer-controls-intro, .deadline-controls-intro { margin: 0 0 14px; color: var(--muted); font-size: 12px; }
.transfer-control-grid, .deadline-control-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.transfer-control-card, .deadline-control-card { min-width: 0; padding: 15px; border: 1px solid var(--line); border-radius: 12px; background: #fbfcfe; }
.transfer-control-card h3, .deadline-control-card h3 { margin: 0 0 5px; color: #23344f; font-size: 14px; }
.transfer-control-card p, .deadline-control-card p { margin: 0 0 11px; color: var(--muted); font-size: 12px; }
.transfer-control-card form, .deadline-control-card form { display: grid; gap: 11px; min-width: 0; margin: 0; }
.transfer-control-card label, .deadline-control-card label { display: grid; gap: 5px; font-size: 13px; font-weight: 650; }
.transfer-control-card select, .deadline-control-card select, .deadline-control-card input { width: 100%; min-height: 40px; padding: 8px 10px; }
.deadline-control-card .muted { margin: 0; }
.transfer-control-empty { padding: 10px 12px; border-radius: 9px; background: #f1f4f8; }
.transfer-controls button { justify-self: start; }
.metadata-editor { margin-top: 12px; padding: 0 16px; }
.metadata-editor summary { padding: 13px 0; cursor: pointer; color: var(--blue); font-size: 13px; font-weight: 700; }
.metadata-editor form { border-top: 1px solid var(--line); padding: 6px 0 14px; }
.metadata-editor[open] summary { border-bottom: 1px solid var(--line); }
.form-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.create-main { max-width: 760px; margin: 0 auto; }
.source-panel, .create-form { padding: 18px 20px; margin: 12px 0; }
.source-panel h2, .create-form h2 { font-size: 16px; margin-bottom: 13px; }
.create-form label, .metadata-editor label { font-size: 13px; }
.intake-review-page .create-form { display: grid; gap: 10px; padding: 16px; }
.intake-review-page .create-form > label { margin: 0; }
.intake-review-page .create-form > button { justify-self: start; margin: 0; }
.intake-review-hint { margin: -5px 0 0; font-size: 12px; }
.intake-key-fields { display: grid; grid-template-columns: 1.1fr 1.1fr 1fr .85fr; align-items: end; gap: 10px; }
.intake-key-fields label { min-width: 0; margin: 0; }
.intake-readonly-field { display: grid; grid-template-rows: auto 42px; gap: 5px; min-width: 0; color: #344054; font-size: 13px; font-weight: 600; }
.intake-readonly-field span { color: var(--ink); }
.intake-readonly-field strong { display: flex; align-items: center; min-width: 0; padding: 0 10px; overflow-wrap: anywhere; border: 1px solid var(--line); border-radius: 9px; background: #f8fafc; color: #52627b; font-size: 13px; font-weight: 500; }
.issue-intake-page { min-height: 100vh; display: grid; align-items: center; }
.issue-intake-page .intake-page { width: 100%; }
.intake-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0 0 10px; }
.intake-heading h1 { margin: 0; font-size: 22px; }
.intake-back-link { display: inline-flex; align-items: center; min-height: 36px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 9px; color: #52627b; background: #fff; font-size: 13px; font-weight: 650; }
.intake-back-link:hover { border-color: #b6c9f3; color: var(--blue); background: var(--blue-soft); }
.intake-form { padding: 14px; margin: 0; }
.intake-form > textarea { min-height: min(38vh, 340px); margin: 0; padding: 14px; resize: vertical; }
.intake-form > textarea::placeholder { color: #98a2b3; }
.intake-form > textarea.drag-active { border-color: #155eef; background: #eff4ff; box-shadow: 0 0 0 3px #dce8ff; }
.intake-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 9px; }
.intake-actions button { min-width: 88px; margin: 0; font-weight: 700; }
.intake-queue { margin: 0; }
.intake-queue select { width: auto; max-width: 220px; min-height: 40px; margin: 0; padding: 7px 28px 7px 10px; font-size: 13px; }
.visually-hidden { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.image-upload-control.intake-image-control { position: relative; margin: 0 auto 0 0; }
.intake-image-control > label { display: inline-flex; align-items: center; min-height: 40px; margin: 0; padding: 7px 11px; border: 1px solid var(--line); border-radius: 9px; color: #34445d; background: #fff; font-weight: 650; cursor: pointer; }
.intake-image-control > label:hover { background: #f8fafc; border-color: #98a2b3; }
.intake-image-control > label:focus-within { outline: 3px solid #dce8ff; border-color: #789af2; }
.intake-image-control .intake-image-input { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.intake-image-control .image-upload-list { margin: 8px 0 0; }
.intake-image-control .image-upload-error { max-width: 420px; margin: 5px 0 0; }
.intake-form .draft-submit-status { margin-bottom: 0; }
.image-upload-control { margin: 12px 0; }
.image-upload-control > label { margin-bottom: 5px; }
.image-upload-control input[type="file"] { margin-top: 0; }
.image-drop-zone { margin-top: 8px; padding: 18px; border: 1px dashed #98a2b3; border-radius: 10px; background: #fbfcff; color: #667085; text-align: center; }
.image-drop-zone.drag-active { border-color: #155eef; background: #eff4ff; color: #155eef; }
.intake-source { margin: 0; padding: 0 12px; box-shadow: none; }
.intake-source summary { padding: 10px 0; color: var(--blue); font-size: 13px; font-weight: 700; cursor: pointer; }
.intake-source[open] summary { border-bottom: 1px solid var(--line); }
.intake-source-content { padding: 8px 0 10px; }
.intake-source-content > .muted { margin: 0 0 8px; }
.intake-part { margin: 8px 0; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; }
.intake-image { display: block; max-height: 420px; margin: 10px auto; object-fit: contain; }
.intake-secondary { margin-top: 16px; }
.image-upload-hint { margin: 5px 0 0; font-size: 12px; }
.image-upload-list { display: grid; gap: 6px; margin: 9px 0 0; padding: 0; list-style: none; }
.image-upload-item { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 8px; background: #fbfcfe; font-size: 12px; }
.image-upload-item span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.image-upload-remove { flex: 0 0 auto; min-height: 30px; margin: 0; padding: 4px 8px; font-size: 12px; }
.image-upload-error { margin: 7px 0 0; color: var(--red); font-size: 12px; }
.timeline-section { margin-top: 24px; }
.section-heading { border-bottom: 1px solid var(--line); margin-bottom: 8px; }
.timeline { position: relative; padding: 4px 0 4px 20px; }
.timeline::before { content: ""; position: absolute; top: 11px; bottom: 20px; left: 5px; width: 2px; background: #e4eaf2; }
.entry {
  position: relative;
  margin: 13px 0;
  padding: 13px 15px;
  border: 1px solid var(--line);
  border-left: 3px solid #8fb5ff;
  border-radius: 11px;
  background: #fff;
}
.entry::before { content: ""; position: absolute; top: 17px; left: -22px; width: 9px; height: 9px; border: 2px solid #fff; border-radius: 50%; background: #5b85ed; box-shadow: 0 0 0 1px #b4c7f5; }
.entry.side-dev { border-left-color: #47a87b; }
.entry.side-dev::before { background: #26a269; box-shadow: 0 0 0 1px #a4d8bd; }
.entry.system { border-left-color: #b3bdca; background: #f9fafc; }
.entry.system::before { background: #8592a5; box-shadow: 0 0 0 1px #c4ccd7; }
.entry-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.entry-author { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; min-width: 0; }
.entry-author strong { font-size: 13px; }
.entry-author span, .entry-heading time { color: var(--muted); font-size: 11px; }
.entry-heading time { flex: 0 0 auto; font-variant-numeric: tabular-nums; }
.entry-chips { display: flex; gap: 5px; flex-wrap: wrap; margin: 7px 0; }
.delivery-badge { padding: 3px 7px; font-size: 10px; }
.delivery-pending { color: #805000; background: #fff2cc; }
.delivery-sent { color: #087a54; background: #e5f7ef; }
.delivery-failed { color: #b42318; background: #fee4e2; }
.entry-intent { padding: 3px 7px; color: #52627b; background: #eef1f5; font-size: 10px; }
.entry-description { margin: 6px 0 2px; color: #52627b; font-size: 12px; }
.entry-content .part { margin: 8px 0; line-height: 1.65; overflow-wrap: anywhere; }
.entry-content img { display: block; max-height: 460px; margin: 9px 0; object-fit: contain; }
.status-queue-main { max-width: 1040px; margin: 0 auto; }
.queue-section { padding: 18px 20px; margin: 14px 0; }
.queue-stack, .status-history { display: grid; gap: 9px; }
.queue-card, .status-change-card { min-width: 0; padding: 12px 14px; border: 1px solid var(--line); border-radius: 11px; background: #fbfcfe; }
.queue-card-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.queue-card-heading a, .status-change-card a { color: var(--ink); font-size: 13px; font-weight: 750; overflow-wrap: anywhere; }
.queue-card p, .status-change-card p { margin: 6px 0 0; color: var(--muted); font-size: 12px; }
.queue-error { color: var(--red) !important; overflow-wrap: anywhere; }
.draft-submit-status { display: flex; align-items: center; gap: 9px; margin: 11px 0 0; font-size: 13px; }
.draft-submit-status[hidden] { display: none; }
.draft-loading-spinner { width: 16px; height: 16px; flex: 0 0 auto; border: 2px solid #c8d7ff; border-top-color: var(--blue); border-radius: 50%; animation: draft-spin .8s linear infinite; }
button.primary:disabled { opacity: .72; cursor: wait; }
@keyframes draft-spin { to { transform: rotate(360deg); } }
.queue-state { flex: 0 0 auto; padding: 3px 8px; border-radius: 999px; font-size: 11px; font-weight: 750; }
.queue-pending { color: #805000; background: #fff2cc; }
.queue-processing { color: #155eef; background: #eaf1ff; }
.queue-retry { color: #b42318; background: #fee4e2; }
.status-change-card { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.status-change-card > div { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.status-change-field { padding: 3px 7px; border-radius: 999px; color: #52627b; background: #eef1f5; font-size: 10px; font-weight: 700; }
.status-change-card p { margin: 0; color: #34445d; white-space: nowrap; }
.status-change-card small { flex: 0 0 auto; color: var(--muted); font-size: 11px; }
.action-panel { padding: 18px 20px; margin-top: 20px; }
.action-panel section h2 { font-size: 16px; }
.action-panel form { margin: 0; }
.action-panel form + form { border-top: 1px solid var(--line); margin-top: 14px; padding-top: 14px; }
.empty { padding: 28px 18px; border: 1px dashed #cbd5e1; border-radius: 12px; text-align: center; background: #fff; }
.admin-main { max-width: 1160px; }
.setup-card { border-color: var(--line); box-shadow: 0 14px 40px #203b690e; }
button, .button-link { min-height: 40px; border-radius: 9px; }
input, select, textarea { border-color: #cbd5e1; border-radius: 9px; }
input:focus, select:focus, textarea:focus { outline: 3px solid #dce8ff; border-color: #789af2; }
button.primary { background: var(--blue); }
@media (max-width: 760px) {
  body.event-page, body.case-detail-page, body.case-create-page, body.status-queue-page { padding: 18px 14px 34px; }
  .topbar { align-items: flex-start; flex-direction: column; gap: 10px; }
  .topbar nav { justify-content: flex-start; margin: 0; }
  .overview-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
  .overview-card { padding: 12px; }
  .case-titlebar { flex-direction: column; }
  .case-state-badges { justify-content: flex-start; }
  .case-meta-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .transfer-control-grid, .deadline-control-grid { grid-template-columns: 1fr; }
  .form-row { grid-template-columns: 1fr; gap: 0; }
  .intake-key-fields { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 480px) {
  body { padding: 16px 12px; }
  .topbar h1 { font-size: 26px; }
  .topbar nav { gap: 2px; }
  .topbar nav a { padding: 7px 8px; font-size: 12px; }
  .priority-filters { gap: 2px; }
  .priority-filters a { padding: 5px 7px; font-size: 12px; }
  .case-card { padding: 14px; }
  .case-meta-grid { grid-template-columns: 1fr 1fr; gap: 12px; }
  .case-overview, .source-panel, .create-form, .action-panel, .transfer-controls, .deadline-controls { padding: 15px; }
  .entry { padding: 11px 12px; }
  .entry-heading { flex-direction: column; gap: 2px; }
  .entry-heading time { font-size: 10px; }
  .case-card-meta { align-items: flex-start; flex-direction: column; }
  .status-change-card { align-items: flex-start; flex-direction: column; gap: 5px; }
  .status-change-card p { white-space: normal; }
}
"""


def _wecom_group_context_scripts(config_url: str) -> str:
    """Return a best-effort WeCom toolbar lookup script for the intake form."""
    config_url_literal = json.dumps(config_url).replace("<", "\\u003c")
    return """
    <script>
    (() => {
      const configEndpoint = __CONFIG_URL__;
      const sdkScripts = [
        'https://res.wx.qq.com/open/js/jweixin-1.2.0.js',
        'https://open.work.weixin.qq.com/wwopen/js/jwxwork-1.0.0.js'
      ];
      const loadScript = (src) => new Promise((resolve, reject) => {
        if (document.querySelector(`script[src="${src}"]`)) {
          if (window.wx) { resolve(); return; }
        }
        const script = document.createElement('script');
        script.src = src;
        script.async = true;
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
      });
      const invoke = (method) => new Promise((resolve) => {
        let finished = false;
        const timer = window.setTimeout(() => finish(null), 4000);
        const finish = (value) => {
          if (finished) return;
          finished = true;
          window.clearTimeout(timer);
          resolve(value);
        };
        try {
          window.wx.invoke(method, {}, (result) => finish(result || null));
        } catch (_) {
          finish(null);
        }
      });
      window.getWeComCustomerGroupId = () => {
        const emptyResult = (status, error = '') => ({ chatId: '', status, error });
        const sanitizeSdkError = (value) => typeof value === 'string'
          ? value.replace(/[^A-Za-z0-9_.:-]+/g, '_').slice(0, 100)
          : '';
        const sdkErrorDetail = (result, method) => {
          const value = result && (result.err_msg || result.errMsg || result.errmsg);
          if (typeof value === 'string' && value) return sanitizeSdkError(value);
          const keys = result && typeof result === 'object'
            ? Object.keys(result).sort().slice(0, 8).join('_')
            : 'no_result_object';
          return `sdk_error_field_empty_keys_${sanitizeSdkError(keys) || 'none'}_${method}`;
        };
        const sdkCallSucceeded = (result, method) => Boolean(result && [
          result.err_msg,
          result.errMsg,
          result.errmsg
        ].includes(`${method}:ok`));
        const reportResult = (result) => {
          window.wecomCustomerGroupLookupStatus = result.status;
          window.wecomCustomerGroupLookupError = result.error || '';
          try {
            if (window.console && typeof window.console.info === 'function') {
              window.console.info('[wecom-group-context]', result.status, result.error || '');
            }
          } catch (_) {}
          return result.chatId;
        };
        if (!/wxwork/i.test(navigator.userAgent || '')) {
          return Promise.resolve(reportResult(emptyResult('not_wecom_client')));
        }
        const lookup = async () => {
          try {
            try {
              for (const src of sdkScripts) await loadScript(src);
            } catch (_) {
              return emptyResult('sdk_script_load_failed');
            }
            if (!window.wx || typeof window.wx.agentConfig !== 'function') {
              return emptyResult('sdk_api_unavailable');
            }
            const pageUrl = window.location.href.split('#', 1)[0];
            let response;
            try {
              response = await fetch(
                `${configEndpoint}?url=${encodeURIComponent(pageUrl)}`,
                { credentials: 'same-origin' }
              );
            } catch (_) {
              return emptyResult('js_sdk_config_fetch_failed');
            }
            if (!response.ok) return emptyResult('js_sdk_config_http_error');
            let config;
            try {
              config = await response.json();
            } catch (_) {
              return emptyResult('js_sdk_config_invalid');
            }
            if (!config || !config.corpid || !config.agentid || !config.signature) {
              return emptyResult('js_sdk_config_invalid');
            }
            let contextStatus = 'agent_config_timeout';
            let contextError = '';
            const context = await new Promise((resolve) => {
              let finished = false;
              const timer = window.setTimeout(() => finish(null), 5000);
              const finish = (value) => {
                if (finished) return;
                finished = true;
                window.clearTimeout(timer);
                resolve(value);
              };
              try {
                window.wx.agentConfig({
                  corpid: config.corpid,
                  agentid: config.agentid,
                  timestamp: config.timestamp,
                  nonceStr: config.nonceStr,
                  signature: config.signature,
                  jsApiList: ['getContext', 'getCurExternalChat'],
                  success: async () => {
                    const result = await invoke('getContext');
                    contextStatus = sdkCallSucceeded(result, 'getContext')
                      ? 'context_ready'
                      : 'get_context_failed';
                    if (contextStatus === 'get_context_failed') {
                      contextError = sdkErrorDetail(result, 'getContext');
                    }
                    finish(sdkCallSucceeded(result, 'getContext') ? result : null);
                  },
                  fail: (error) => {
                    contextStatus = 'agent_config_failed';
                    contextError = sdkErrorDetail(error, 'agentConfig');
                    finish(null);
                  }
                });
              } catch (_) {
                contextStatus = 'agent_config_failed';
                finish(null);
              }
            });
            if (!context) return emptyResult(contextStatus, contextError);
            if (context.entry !== 'group_chat_tools') {
              return emptyResult('not_group_chat_tools');
            }
            const group = await invoke('getCurExternalChat');
            if (!group) return emptyResult('get_cur_external_chat_timeout');
            if (!sdkCallSucceeded(group, 'getCurExternalChat')) {
              return emptyResult(
                'get_cur_external_chat_failed',
                sdkErrorDetail(group, 'getCurExternalChat')
              );
            }
            if (typeof group.chatId !== 'string' || !group.chatId.trim()) {
              return emptyResult('chat_id_missing');
            }
            return { chatId: group.chatId, status: 'success' };
          } catch (_) {
            return emptyResult('lookup_exception');
          }
        };
        return Promise.race([
          lookup(),
          new Promise((resolve) => window.setTimeout(
            () => resolve(emptyResult('lookup_timeout')),
            9000
          ))
        ]).then(reportResult);
      };
    })();
    </script>
    """.replace("__CONFIG_URL__", config_url_literal)


def _page(title: str, body: str, *, body_class: str = "") -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>
body{{font:16px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:760px;margin:0 auto;padding:20px;color:#172033;background:#f5f7fb}}
a{{color:#1769e0;text-decoration:none}}
nav{{display:flex;gap:14px;margin-bottom:18px;flex-wrap:wrap}}
h1,h2,h3,p{{margin-top:0}}
h1{{font-size:28px;letter-spacing:-.02em;margin-bottom:6px}}
h2{{font-size:20px;margin-bottom:6px}}
h3{{font-size:17px;margin:3px 0 0}}
.topbar{{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:20px}}
.topbar nav{{margin:10px 0 0;justify-content:flex-end}}
.topbar nav a{{padding:7px 10px;border-radius:8px;color:#667085}}
.topbar nav a.active,.topbar nav a:hover{{background:#eaf1ff;color:#155eef}}
.eyebrow{{font-size:11px;line-height:1.2;letter-spacing:.12em;text-transform:uppercase;color:#667085;font-weight:700}}
.case-card{{display:block;border:1px solid #dbe3ef;border-radius:10px;padding:14px;margin:10px 0;color:inherit;background:#fff;box-shadow:0 2px 7px #1720330a}}
.case-card small,.muted{{display:block;color:#667085;margin-top:4px}}
.issue-fields{{margin:14px 0}}
.issue-fields h2{{margin-bottom:12px}}
.issue-fields-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}
.issue-field{{padding:10px;border-radius:8px;background:#f8fafc;overflow-wrap:anywhere}}
.issue-field span{{display:block;margin-bottom:4px;color:#667085;font-size:12px}}
.issue-field strong,.issue-field p{{margin:0;white-space:pre-wrap;line-height:1.55}}
.issue-field-wide{{grid-column:1 / -1}}
.entry{{border-left:3px solid #8fb5ff;padding:8px 12px;margin:14px 0;background:#f8fbff}}
.entry.system{{border-color:#a7b0be;background:#fafafa}}
.part{{white-space:pre-wrap;margin:6px 0}}
img{{max-width:100%;border-radius:8px}}
label{{display:block;margin:12px 0;color:#344054;font-size:14px;font-weight:600}}
input,select,textarea{{display:block;width:100%;box-sizing:border-box;padding:10px 11px;margin-top:5px;border:1px solid #cbd5e1;border-radius:8px;background:#fff;color:#172033;font:inherit}}
textarea{{min-height:110px;resize:vertical}}
button,.button-link{{display:inline-block;padding:9px 13px;margin:6px 4px 6px 0;border:1px solid #cbd5e1;border-radius:8px;background:#fff;color:#344054;font:inherit;cursor:pointer}}
button:hover,.button-link:hover{{border-color:#98a2b3;background:#f8fafc}}
button.primary,.primary{{border-color:#155eef;background:#155eef;color:#fff}}
button.primary:hover{{background:#004eeb}}
.empty{{color:#667085}}
.notice{{padding:12px 14px;border-radius:9px;background:#fff7e8;color:#8a4b08;border:1px solid #f6d28b;margin:16px 0}}
.setup-card{{max-width:540px;margin:10vh auto;background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:28px;box-shadow:0 12px 30px #17203312}}
.setup-card .muted{{line-height:1.65}}
.event-list{{max-width:680px}}
.admin-page{{max-width:1160px;background:#f5f7fb}}
.admin-main{{max-width:1160px;margin:0 auto}}
.admin-topbar{{padding-bottom:6px}}
.admin-topbar .muted{{font-size:13px}}
.hero-banner{{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;padding:17px 20px;margin-bottom:18px;border:1px solid #c7d7fe;background:#eef4ff;border-radius:14px;color:#193b8f}}
.hero-banner p{{margin:7px 0 0;color:#52618a;font-size:14px;line-height:1.55}}
.status-dot{{display:inline-block;width:9px;height:9px;margin-right:8px;border-radius:50%;background:#12b76a;box-shadow:0 0 0 4px #d1fadf;vertical-align:1px}}
.status-dot.small{{width:7px;height:7px;margin-right:7px;box-shadow:none;vertical-align:1px}}
.status-dot.off{{background:#98a2b3}}
.badge,.count-pill,.tag{{display:inline-flex;align-items:center;white-space:nowrap;border-radius:999px;font-size:12px;font-weight:700}}
.badge{{padding:4px 8px;background:#f2f4f7;color:#475467}}
.badge-blue{{background:#dbe8ff;color:#155eef}}
.badge-green{{background:#d1fadf;color:#067647}}
.badge-amber{{background:#fef0c7;color:#b54708}}
.badge-gray{{background:#eaecf0;color:#667085}}
.count-pill{{padding:4px 9px;background:#f2f4f7;color:#667085}}
.stats-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:18px}}
.stat-card,.panel,.permission-note{{border:1px solid #e2e8f0;background:#fff;border-radius:14px;box-shadow:0 3px 10px #17203308}}
.stat-card{{padding:15px 16px}}
.stat-card span,.stat-card small{{display:block;color:#667085;font-size:13px}}
.stat-card strong{{display:block;margin:8px 0 4px;font-size:28px;letter-spacing:-.03em}}
.admin-grid{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px;align-items:start}}
.panel{{padding:20px}}
.panel-heading,.team-header,.member-heading{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}}
.panel-intro{{color:#667085;font-size:13px;line-height:1.6;margin:12px 0 16px}}
.search-form{{display:flex;gap:8px;margin-bottom:13px}}
.search-form input{{margin:0;min-width:0}}
.search-form button{{margin:0}}
.button-link{{margin:0;padding:9px 10px}}
.add-box{{border:1px dashed #b8c4d6;border-radius:10px;padding:0 12px;margin:13px 0 15px;background:#fbfcfe}}
.add-box summary{{padding:12px 0;cursor:pointer;font-weight:700;color:#155eef}}
.form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:0 12px;padding-bottom:9px}}
.form-grid label:first-child{{grid-column:1/-1}}
.form-grid .primary,.form-grid button{{grid-column:1/-1;justify-self:start}}
.check-label{{display:flex;align-items:center;gap:7px;font-size:13px!important;font-weight:500!important}}
.check-label input{{width:auto;margin:0}}
.managed-list{{display:flex;flex-direction:column;gap:8px}}
.managed-row{{border:1px solid #e6eaf0;border-radius:10px;background:#fff;overflow:hidden}}
.managed-row summary{{display:flex;align-items:center;gap:8px;list-style:none;padding:11px 12px;cursor:pointer}}
.managed-row summary::-webkit-details-marker{{display:none}}
.avatar{{display:inline-grid;place-items:center;flex:0 0 31px;width:31px;height:31px;border-radius:9px;background:#eaf1ff;color:#155eef;font-weight:800}}
.user-summary{{display:flex;flex:1;min-width:0;flex-direction:column}}
.user-summary strong{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.user-summary small,.member-row small{{display:block;color:#98a2b3;font-size:11px;margin-top:2px}}
.chevron{{color:#98a2b3;font-size:18px}}
.managed-detail{{padding:0 12px 12px;border-top:1px solid #eef0f3}}
.tag-list{{display:flex;flex-wrap:wrap;gap:5px;margin-top:10px}}
.tag{{padding:4px 7px;background:#ecfdf3;color:#067647;font-weight:600}}
.tag-muted{{background:#f2f4f7;color:#98a2b3}}
.edit-form{{display:grid;grid-template-columns:1fr auto auto;align-items:end;gap:7px;margin-top:7px}}
.edit-form label{{margin-bottom:0}}
.edit-form .check-label{{margin-bottom:10px}}
.edit-form button{{margin:0}}
.team-card{{border:1px solid #e6eaf0;border-radius:10px;padding:13px;margin-bottom:10px}}
.team-header h3{{margin-top:4px}}
.team-edit-form{{display:flex;gap:7px;align-items:center;margin:11px 0 3px}}
.team-edit-form input{{margin:0;min-width:0;flex:1}}
.team-edit-form .check-label{{margin:0;white-space:nowrap}}
.team-edit-form button{{margin:0}}
.subtle-button{{padding:7px 9px;font-size:12px}}
.danger-button{{padding:7px 9px;font-size:12px;color:#b42318;border-color:#fecdca;background:#fffafa}}
.channel-box{{margin:12px 0;padding:11px;border-radius:9px;background:#f8fafc}}
.channel-box strong,.channel-box .muted,.channel-box .warning-text{{display:block;font-size:12px}}
.warning-text{{color:#b54708;margin-top:4px}}
.inline-form{{display:flex;gap:6px;align-items:center;margin-top:8px}}
.inline-form input{{margin:0;min-width:0;flex:1}}
.inline-form button{{margin:0}}
.member-heading{{margin-top:13px;padding-bottom:7px;border-bottom:1px solid #eef0f3;font-size:13px}}
.member-heading span{{color:#98a2b3;font-size:12px}}
.member-list{{margin-bottom:8px}}
.member-row{{display:flex;justify-content:space-between;gap:7px;align-items:center;padding:9px 0;border-bottom:1px solid #f1f3f5}}
.member-row>div:first-child{{display:flex;align-items:center;min-width:0}}
.member-row>div:first-child>span:last-child{{min-width:0}}
.member-row strong{{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}}
.member-actions{{display:flex;gap:4px;align-items:center;flex-shrink:0}}
.member-actions form{{margin:0}}
.role-form{{display:flex;gap:4px;align-items:center}}
.role-form select{{margin:0;padding:6px 7px;font-size:12px}}
.role-form button,.member-actions .danger-button{{margin:0}}
.add-member-form{{display:grid;grid-template-columns:1.5fr 1fr auto;gap:5px;margin-top:10px}}
.add-member-form select{{margin:0;min-width:0;padding:8px 7px;font-size:12px}}
.add-member-form button{{margin:0;padding:8px 10px;font-size:12px}}
.permission-note{{display:flex;gap:12px;align-items:flex-start;margin-top:18px;padding:14px 17px;color:#667085;font-size:13px;line-height:1.6}}
.permission-note strong{{color:#344054;white-space:nowrap}}
@media (max-width:800px){{body{{padding:14px}}.admin-grid{{grid-template-columns:1fr}}.stats-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
@media (max-width:520px){{.topbar{{flex-direction:column}}.topbar nav{{justify-content:flex-start;margin-top:0}}.hero-banner{{padding:14px}}.form-grid,.edit-form{{grid-template-columns:1fr}}.form-grid label:first-child,.form-grid .primary,.form-grid button{{grid-column:auto}}.edit-form button{{justify-self:start}}.member-row{{align-items:flex-start;flex-direction:column}}.member-actions{{width:100%;justify-content:flex-end}}.add-member-form{{grid-template-columns:1fr 1fr}}.add-member-form button{{grid-column:1/-1;justify-self:start}}.permission-note{{flex-direction:column;gap:3px}}}}
body.setup-page{{background:#eef4ff}}
{_WORKBENCH_CSS}
</style></head><body class="{escape(body_class)}">{body}</body></html>"""


def _image_upload_controls(*, compact: bool = False) -> str:
    controls = (
        """
    <div class="image-upload-control intake-image-control" data-image-uploader="true" data-max-image-bytes="20971520">
      <label for="issue-images">＋ 图片
        <input id="issue-images" class="intake-image-input" name="images" type="file" accept="image/*" multiple
          data-image-input="true" aria-label="添加问题图片">
      </label>
      <ul class="image-upload-list" data-image-list="true" aria-live="polite"></ul>
      <p class="image-upload-error" data-image-error="true" role="alert" hidden></p>
    </div>
    """
        if compact
        else """
    <div class="image-upload-control" data-image-uploader="true" data-max-image-bytes="20971520">
      <label for="issue-images">问题图片（可拖拽、多选或粘贴，单张不超过 20 MiB）</label>
      <input id="issue-images" name="images" type="file" accept="image/*" multiple
        data-image-input="true" aria-describedby="issue-images-hint">
      <div class="image-drop-zone" data-image-drop-zone="true">
        将图片拖到此处，或用上方按钮选择文件
      </div>
      <p id="issue-images-hint" class="muted image-upload-hint">
        图片会按 [图片] 占位符顺序匹配；未匹配的多余图片会追加到末尾。
      </p>
      <ul class="image-upload-list" data-image-list="true" aria-live="polite"></ul>
      <p class="image-upload-error" data-image-error="true" role="alert" hidden></p>
    </div>
    """
    )
    return controls + """
    <script>
    (() => {
      const uploader = document.querySelector('[data-image-uploader="true"]');
      if (!uploader) return;
      const input = uploader.querySelector('[data-image-input="true"]');
      const list = uploader.querySelector('[data-image-list="true"]');
      const error = uploader.querySelector('[data-image-error="true"]');
      const form = input && input.form;
      const dropZone = form?.querySelector('[data-image-drop-zone="true"]')
        || uploader.querySelector('[data-image-drop-zone="true"]');
      if (!input || !list || !error || !form) return;

      const maxBytes = Number(uploader.dataset.maxImageBytes);
      let selectedFiles = [];
      let canSynchronizeFiles = typeof DataTransfer !== "undefined";

      const fileKey = (file) =>
        [file.name, file.size, file.lastModified, file.type].join(":");

      const showError = (message) => {
        error.textContent = message;
        error.hidden = !message;
      };

      const syncInputFiles = () => {
        if (!canSynchronizeFiles) return false;
        try {
          const transfer = new DataTransfer();
          selectedFiles.forEach((file) => transfer.items.add(file));
          input.files = transfer.files;
          return true;
        } catch (_error) {
          canSynchronizeFiles = false;
          return false;
        }
      };

      const renderFiles = () => {
        list.replaceChildren();
        selectedFiles.forEach((file, index) => {
          const item = document.createElement("li");
          item.className = "image-upload-item";
          const name = document.createElement("span");
          name.textContent = file.name || `图片 ${index + 1}`;
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "image-upload-remove";
          remove.textContent = "移除";
          remove.setAttribute("aria-label", `移除 ${name.textContent}`);
          remove.addEventListener("click", () => {
            selectedFiles.splice(index, 1);
            syncInputFiles();
            renderFiles();
          });
          item.append(name, remove);
          list.append(item);
        });
      };

      const addFiles = (incoming) => {
        const existing = new Set(selectedFiles.map(fileKey));
        const rejected = [];
        for (const file of incoming) {
          if (!file || !file.type.startsWith("image/")) {
            rejected.push(`${file?.name || "文件"} 不是图片`);
            continue;
          }
          if (file.size > maxBytes) {
            rejected.push(`${file.name || "图片"} 超过 20 MiB`);
            continue;
          }
          const key = fileKey(file);
          if (!existing.has(key)) {
            existing.add(key);
            selectedFiles.push(file);
          }
        }
        syncInputFiles();
        renderFiles();
        showError(rejected.length ? rejected.join("；") : "");
      };

      input.addEventListener("change", () => {
        const picked = Array.from(input.files || []);
        if (!picked.length) return;
        if (canSynchronizeFiles) {
          input.value = "";
          addFiles(picked);
        } else {
          selectedFiles = picked;
          renderFiles();
        }
      });

      if (dropZone) {
        ["dragenter", "dragover"].forEach((name) => {
          dropZone.addEventListener(name, (event) => {
            event.preventDefault();
            dropZone.classList.add("drag-active");
          });
        });
        ["dragleave", "drop"].forEach((name) => {
          dropZone.addEventListener(name, (event) => {
            event.preventDefault();
            dropZone.classList.remove("drag-active");
          });
        });
        dropZone.addEventListener("drop", (event) => {
          addFiles(Array.from(event.dataTransfer?.files || []));
        });
      }

      form.addEventListener("paste", (event) => {
        const items = Array.from(event.clipboardData?.items || []);
        const pastedFiles = items
          .filter((item) => item.kind === "file")
          .map((item) => item.getAsFile())
          .filter(Boolean);
        if (!pastedFiles.length) return;
        event.preventDefault();
        addFiles(pastedFiles);
      });

      form.addEventListener("submit", () => {
        syncInputFiles();
      });
    })();
    </script>
    """


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")


def _datetime_local_value(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%dT%H:%M")


def _date_local_value(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def _time_local_value(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")


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


def _hours_value(minutes: int) -> str:
    return format((Decimal(minutes) / 60).normalize(), "f")


def _duration_label(minutes: int) -> str:
    hours, remaining_minutes = divmod(minutes, 60)
    parts = [f"{hours}小时"] if hours else []
    if remaining_minutes:
        parts.append(f"{remaining_minutes}分钟")
    return "".join(parts) or "0分钟"


def _case_status_label(status: CaseStatus) -> str:
    return {
        CaseStatus.PENDING_CONFIRMATION: "待确定",
        CaseStatus.IN_PROGRESS: "处理中",
        CaseStatus.WAITING_CUSTOMER: "待客户反馈",
        CaseStatus.CLOSED: "关闭",
        CaseStatus.SUSPENDED: "挂起",
    }[status]


def _status_queue_card(item: Any, path_for: Callable[[str], str]) -> str:
    case_ref = escape(item.case_ref)
    issue_path = path_for(f"/issues/{case_ref}")
    state_label, state_class = (
        ("处理中", "queue-processing")
        if item.state == "processing"
        else (("等待重试", "queue-retry") if item.attempts else ("排队中", "queue-pending"))
    )
    retry = (
        f'<p class="queue-error">最近错误：{escape(item.last_error[:300])}</p>'
        if item.last_error
        else ""
    )
    attempts = f" · 已重试 {item.attempts} 次" if item.attempts else ""
    retry_at = f" · 下次重试 {_format_datetime(item.next_attempt_at)}" if item.attempts else ""
    return (
        '<article class="queue-card">'
        f'<div class="queue-card-heading"><a href="{issue_path}">'
        f'{_status_queue_issue_label(item)} · {escape(item.title)}</a>'
        f'<span class="queue-state {state_class}">{state_label}</span></div>'
        f'<p>触发沟通序号 {item.entry_sequence} · 入队 {_format_datetime(item.created_at)}'
        f'{attempts}{retry_at}</p>{retry}</article>'
    )


def _status_queue_issue_label(item: Any) -> str:
    bug_number = escape(item.bug_number or "待生成")
    customer_name = escape(item.customer_name or "未录入")
    return f"BUG 编号 {bug_number} · 客户 {customer_name}"


def _issue_draft_queue_card(item: Any) -> str:
    return (
        '<article class="queue-card">'
        '<div class="queue-card-heading"><strong>Agent 整理问题草稿</strong>'
        '<span class="queue-state queue-processing">处理中</span></div>'
        f'<p>快速上传 · 开始时间 {_format_datetime(item.started_at)}</p>'
        '</article>'
    )


def _recent_status_change_card(item: Any, path_for: Callable[[str], str]) -> str:
    case_ref = escape(item.case_ref)
    issue_path = path_for(f"/issues/{case_ref}")
    previous = item.from_status
    updated = item.to_status
    if item.field_label == "协作流程状态":
        try:
            previous = _case_status_label(CaseStatus(previous))
            updated = _case_status_label(CaseStatus(updated))
        except ValueError:
            pass
    return (
        '<article class="status-change-card">'
        f'<div><a href="{issue_path}">{_status_queue_issue_label(item)} · {escape(item.title)}</a>'
        f'<span class="status-change-field">{escape(item.field_label)}</span></div>'
        f'<p>{escape(previous)} <span aria-hidden="true">→</span> {escape(updated)}</p>'
        f'<small>{escape(item.actor_name)} · {_format_datetime(item.created_at)}</small>'
        '</article>'
    )


def _deadline_badge(status: DeadlineStatus | None) -> str:
    if status is None:
        return ""
    label, color = {
        DeadlineStatus.APPROACHING: ("临近期限", "deadline-approaching"),
        DeadlineStatus.OVERDUE: ("已超时", "deadline-overdue"),
    }[status]
    return f'<span class="status-badge {color}">{label}</span>'


def _priority_badge(priority: CasePriority) -> str:
    label, color = {
        CasePriority.NORMAL: ("普通", "priority-normal"),
        CasePriority.URGENT: ("紧急", "priority-urgent"),
        CasePriority.SEVERE: ("严重", "priority-severe"),
    }[priority]
    return f'<span class="status-badge {color}">优先级 · {label}</span>'


def _entry_kind_label(kind: CaseEntryKind) -> str:
    return {
        CaseEntryKind.FORMAL_MESSAGE: "正式沟通",
        CaseEntryKind.TRANSFER_CONSULTANT: "咨询处理人变更",
        CaseEntryKind.TRANSFER_DEVELOPER: "研发处理人变更",
        CaseEntryKind.TRANSFER_DEV_TEAM: "研发团队变更",
        CaseEntryKind.CLOSED: "问题关闭",
        CaseEntryKind.REOPENED: "问题重新打开",
        CaseEntryKind.STATUS_CHANGED: "状态变更",
        CaseEntryKind.CORRECTION: "消息纠错",
        CaseEntryKind.CASE_METADATA_UPDATED: "问题资料变更",
    }[kind]


def _entry_description(entry: EntryView) -> str:
    metadata = entry.metadata
    if entry.kind is CaseEntryKind.CASE_METADATA_UPDATED:
        changes = metadata.get("changes", {})
        labels = {
            "customer_name": "客户/企业",
            "customer_contact_name": "联系人",
            "customer_contact_method": "联系方式",
            "priority": "紧急程度",
            "deadline": "截止时间",
            "approaching_window_minutes": "临近期限提醒提前量",
        }
        descriptions = []
        if isinstance(changes, Mapping):
            for key, change in changes.items():
                if not isinstance(change, Mapping):
                    continue
                before = change.get("from") or "未填写"
                after = change.get("to") or "未填写"
                if key == "priority":
                    before = _priority_label_value(str(before))
                    after = _priority_label_value(str(after))
                elif key == "deadline":
                    before = _format_datetime(datetime.fromisoformat(str(before)))
                    after = _format_datetime(datetime.fromisoformat(str(after)))
                elif key == "approaching_window_minutes":
                    before = _duration_label(int(before))
                    after = _duration_label(int(after))
                descriptions.append(f"{labels.get(str(key), str(key))}：{before} → {after}")
        return "；".join(descriptions) or "问题资料已更新"
    if entry.kind is CaseEntryKind.TRANSFER_CONSULTANT:
        return (
            f"咨询处理人由 {metadata.get('from_consultant') or '未指定'} "
            f"调整为 {metadata.get('to_consultant') or '未指定'}"
        )
    if entry.kind is CaseEntryKind.TRANSFER_DEVELOPER:
        return (
            f"研发处理人由 {metadata.get('from_developer') or '未指定'} "
            f"调整为 {metadata.get('to_developer') or '未指定'}"
        )
    if entry.kind is CaseEntryKind.TRANSFER_DEV_TEAM:
        from_team = metadata.get("from_dev_team") or "未知团队"
        to_team = metadata.get("to_dev_team") or "未知团队"
        from_developer = metadata.get("from_developer") or "未指定"
        to_developer = metadata.get("to_developer") or "未指定"
        return (
            f"研发团队由 {from_team} 调整为 {to_team}；"
            f"研发处理人由 {from_developer} 调整为 {to_developer}"
        )
    if entry.kind is CaseEntryKind.CLOSED:
        return "咨询侧已确认客户问题闭环"
    if entry.kind is CaseEntryKind.REOPENED:
        waiting = metadata.get("waiting_on")
        next_step = "研发" if waiting == WaitingOn.DEV.value else "咨询"
        return f"问题重新打开并进入“处理中”，下一步由{next_step}处理"
    if entry.kind is CaseEntryKind.STATUS_CHANGED:
        try:
            previous = CaseStatus(str(metadata.get("from_status")))
            updated = CaseStatus(str(metadata.get("to_status")))
        except ValueError:
            return "问题状态已更新"
        return f"问题状态由“{_case_status_label(previous)}”变更为“{_case_status_label(updated)}”"
    if entry.kind is CaseEntryKind.CORRECTION:
        target = metadata.get("corrected_to_case_ref") or metadata.get("corrected_from_case_ref")
        return f"消息关联已更正{f'，目标问题 {target}' if target else ''}"
    return ""


def _priority_label_value(value: str) -> str:
    return {
        CasePriority.NORMAL.value: "普通",
        CasePriority.URGENT.value: "紧急",
        CasePriority.SEVERE.value: "严重",
    }.get(value, value)


def _render_delivery_badges(deliveries: tuple[Any, ...] | list[Any], entry_side: EntrySide) -> str:
    if not deliveries:
        return ""
    chips: list[str] = []
    for delivery in deliveries:
        if delivery.destination_type is DeliveryDestination.CHAT:
            target = "咨询群" if entry_side is EntrySide.DEV else "研发群"
        else:
            target = "咨询处理人" if entry_side is EntrySide.DEV else "咨询侧"
        label, color = {
            DeliveryStatus.PENDING: ("投递中", "delivery-pending"),
            DeliveryStatus.SENT: ("已送达", "delivery-sent"),
            DeliveryStatus.FAILED: ("投递失败", "delivery-failed"),
        }[delivery.status]
        chips.append(f'<span class="delivery-badge {color}">{target} · {label}</span>')
    return "".join(chips)


def _render_entry(
    case_ref: str,
    entry: EntryView,
    path_for: Callable[[str], str],
    deliveries: tuple[Any, ...] | list[Any] = (),
) -> str:
    kind = "system" if entry.side is EntrySide.SYSTEM else f"side-{entry.side.value}"
    side_label = {
        EntrySide.CONSULT: "咨询侧",
        EntrySide.DEV: "研发侧",
        EntrySide.SYSTEM: "问题记录",
    }[entry.side]
    heading = (
        f'<div class="entry-author"><strong>{escape(entry.actor_name_snapshot)}</strong>'
        f"<span>{side_label} · {_entry_kind_label(entry.kind)}</span></div>"
    )
    description = _entry_description(entry)
    detail = f'<p class="entry-description">{escape(description)}</p>' if description else ""
    intent_badge = (
        '<span class="entry-intent">仅同步</span>'
        if entry.message_intent is MessageIntent.SYNC
        else ""
    )
    delivery_badges = _render_delivery_badges(deliveries, entry.side)
    return (
        f'<article class="entry {kind}"><div class="entry-heading">{heading}'
        f'<time datetime="{escape(entry.created_at.isoformat())}">{_format_datetime(entry.created_at)}</time></div>'
        f'<div class="entry-chips">{intent_badge}{delivery_badges}</div>{detail}'
        f'<div class="entry-content">{_render_parts(entry.parts, case_ref, path_for)}</div></article>'
    )


def _render_parts(
    parts: tuple[Any, ...], case_ref: str | None, path_for: Callable[[str], str] | None = None
) -> str:
    rendered: list[str] = []
    for part in parts:
        if part.kind is PartKind.TEXT:
            rendered.append(f'<p class="part">{escape(part.text or "")}</p>')
        elif part.media_id is not None and case_ref is not None:
            assert path_for is not None
            media_path = path_for(f"/issues/{escape(case_ref)}/media/{part.media_id}")
            rendered.append(f'<img alt="问题图片" src="{media_path}">')
        else:
            rendered.append('<p class="part">[图片]</p>')
    return "".join(rendered)


def _issue_module_options(selected: str) -> str:
    values = tuple(module.value for module in IssueModule)
    options = []
    if selected not in values:
        options.append('<option value="" selected disabled>请选择问题所属模块</option>')
    for module in IssueModule:
        is_selected = ' selected' if selected == module.value else ''
        options.append(f'<option value="{escape(module.value)}"{is_selected}>{escape(module.value)}</option>')
    return "".join(options)


def _issue_status_options(selected: str) -> str:
    values = tuple(status.value for status in IssueCurrentStatus)
    options = []
    if selected not in values:
        label = f"历史状态：{selected}（请选择新状态）" if selected else "请选择问题状态"
        options.append(f'<option value="" selected disabled>{escape(label)}</option>')
    for status in IssueCurrentStatus:
        is_selected = ' selected' if selected == status.value else ''
        options.append(f'<option value="{escape(status.value)}"{is_selected}>{escape(status.value)}</option>')
    return "".join(options)


def _issue_current_status_control(case: Any, path_for: Callable[[str], str]) -> str:
    if not case.can_edit_metadata:
        return f"<strong>{escape(case.issue_current_status)}</strong>"
    action = path_for(f"/issues/{escape(case.case_ref)}/issue-current-status")
    options = _issue_status_options(case.issue_current_status)
    return f"""
    <form method="post" action="{action}" class="issue-status-editor">
      <input type="hidden" name="version" value="{case.version}">
      <select name="issue_current_status" required aria-label="选择问题状态">{options}</select>
      <button type="submit">保存状态</button>
    </form>
    """


def _metadata_form(case: Any, path_for: Callable[[str], str]) -> str:
    if not case.can_edit_metadata:
        return ""
    action = path_for(f"/issues/{escape(case.case_ref)}/metadata")
    priority_options = "".join(
        f'<option value="{level.value}"{" selected" if case.priority is level else ""}>'
        f"{label}</option>"
        for level, label in (
            (CasePriority.NORMAL, "普通"),
            (CasePriority.URGENT, "紧急"),
            (CasePriority.SEVERE, "严重"),
        )
    )
    return f"""
    <details class="metadata-editor panel">
      <summary>编辑客户资料与紧急程度</summary>
      <form method="post" action="{action}">
        <input type="hidden" name="version" value="{case.version}">
        <div class="form-row">
          <label>客户 / 企业名称
            <input name="customer_name" required maxlength="256" value="{escape(case.customer_name or "")}">
          </label>
          <label>客户群聊名称（选填）
            <input name="customer_group_name" maxlength="256" value="{escape(case.customer_group_name or "")}">
          </label>
          <label>联系人（选填）
            <input name="customer_contact_name" maxlength="256" value="{escape(case.customer_contact_name or "")}">
          </label>
        </div>
        <div class="form-row">
          <label>联系方式（选填）
            <input name="customer_contact_method" maxlength="256" value="{escape(case.customer_contact_method or "")}">
          </label>
          <label>紧急程度 <select name="priority">{priority_options}</select></label>
        </div>
        <button class="primary" type="submit">保存变更</button>
      </form>
    </details>
    """


def _transfer_controls(case: Any, path_for: Callable[[str], str]) -> str:
    if not case.can_transfer:
        return ""
    consultant_action = path_for(f"/issues/{escape(case.case_ref)}/transfer-consultant")
    consultant_control = _transfer_person_control(
        case=case,
        action=consultant_action,
        field_name="new_consultant_id",
        heading="转交咨询处理人",
        team_name=case.consult_queue_name,
        current_id=case.current_consultant_id,
        people=case.transferable_consultants,
        button_label="转交咨询处理人",
    )
    return f"""
    <section class="transfer-controls panel" aria-label="咨询经办人转交">
      <h2>咨询经办人转交</h2>
      <p class="transfer-controls-intro">当前咨询经办人、咨询队列管理员或研发团队管理员可以转交；
        接收人必须是当前咨询队列中的在岗成员。</p>
      <div class="transfer-control-grid">
        {consultant_control}
      </div>
    </section>
    """


def _transfer_person_control(
    *,
    case: Any,
    action: str,
    field_name: str,
    heading: str,
    team_name: str,
    current_id: UUID | None,
    people: tuple[Any, ...],
    button_label: str,
) -> str:
    candidates = tuple(person for person in people if person.id != current_id)
    if not candidates:
        return f"""
        <div class="transfer-control-card">
          <h3>{escape(heading)}</h3>
          <p>目标范围：{escape(team_name)}</p>
          <p class="transfer-control-empty">当前团队没有其他在岗成员可选。</p>
        </div>
        """
    options = "".join(
        f'<option value="{escape(str(person.id))}">{escape(person.display_name)}</option>'
        for person in candidates
    )
    return f"""
    <div class="transfer-control-card">
      <h3>{escape(heading)}</h3>
      <p>目标范围：{escape(team_name)}；仅显示本团队其他在岗成员。</p>
      <form method="post" action="{action}" aria-label="{escape(heading)}">
        <input type="hidden" name="version" value="{case.version}">
        <label>新的处理人
          <select name="{field_name}" required>
            <option value="" selected disabled>请选择本团队成员</option>{options}
          </select>
        </label>
        <button class="primary" type="submit">{escape(button_label)}</button>
      </form>
    </div>
    """


def _deadline_controls(case: Any, path_for: Callable[[str], str]) -> str:
    if not case.can_extend_deadline or case.lifecycle_status is LifecycleStatus.CLOSED:
        return ""
    adjust_action = path_for(f"/issues/{escape(case.case_ref)}/deadline/adjust")
    approaching_action = path_for(f"/issues/{escape(case.case_ref)}/deadline/approaching-window")
    approaching_hours = _hours_value(case.approaching_window_minutes)
    deadline_date = _date_local_value(case.deadline)
    deadline_time = _time_local_value(case.deadline)
    return f"""
    <section class="deadline-controls panel" aria-label="期限管理">
      <h2>期限管理</h2>
      <p class="deadline-controls-intro">截止时间和临近期限提醒是两项独立设置，分别保存，互不改变。</p>
      <div class="deadline-control-grid">
        <div class="deadline-control-card">
          <h3>调整当前截止时间</h3>
          <p>选择新的截止日期和时间，不会修改临近期限提醒。</p>
          <form method="post" action="{adjust_action}" aria-label="调整截止时间">
            <input type="hidden" name="version" value="{case.version}">
            <label>截止日期
              <input type="date" name="deadline_date" value="{deadline_date}" required>
            </label>
            <label>截止时间（24小时制）
              <input type="time" name="deadline_time" step="60" value="{deadline_time}" required>
            </label>
            <p class="muted">此操作只改变截止时间，不会改变临近期限提醒设置。</p>
            <button class="primary" type="submit">保存截止时间</button>
          </form>
        </div>
        <div class="deadline-control-card">
          <h3>设置临近期限提醒</h3>
          <p>设置距截止时间多久时开始显示“临近期限”，不改变截止时间。</p>
          <form method="post" action="{approaching_action}" aria-label="设置临近期限提醒">
            <input type="hidden" name="version" value="{case.version}">
            <label>提前多久进入临近期限（小时）
              <input type="number" name="approaching_hours" min="0.5" step="0.5" value="{approaching_hours}" required>
            </label>
            <p class="muted">默认提前 24 小时，按至少 30 分钟递增。</p>
            <p class="muted">此操作只改变临近期限提醒设置，不会调整截止时间。</p>
            <button class="primary" type="submit">保存提醒设置</button>
          </form>
        </div>
      </div>
    </section>
    """


def _top_close_control(case: Any, path_for: Callable[[str], str]) -> str:
    if (
        case.lifecycle_status is LifecycleStatus.CLOSED
        or not case.can_change_consult_status
    ):
        return ""
    close_action = path_for(f"/issues/{escape(case.case_ref)}/close")
    return f"""
    <form class="top-close-control" method="post" action="{close_action}">
      <input type="hidden" name="version" value="{case.version}">
      <button type="submit" class="status-action-close">关闭问题</button>
    </form>
    """


def _message_form(
    case_ref: str,
    version: int,
    lifecycle: LifecycleStatus,
    path_for: Callable[[str], str],
    *,
    show_side: bool = False,
) -> str:
    if lifecycle is LifecycleStatus.CLOSED:
        return ""
    message_path = path_for(f"/issues/{escape(case_ref)}/messages")
    side_selector = (
        '<label>发送身份<select name="side">'
        '<option value="consult">咨询</option><option value="dev">研发</option>'
        "</select></label>"
        if show_side
        else ""
    )
    return f"""
    <section><h2>发送正式消息</h2>
    <form method="post" action="{message_path}">
      <input type="hidden" name="version" value="{version}">
      <label>内容<textarea name="text" required></textarea></label>
      {side_selector}
      <label><input type="checkbox" name="intent" value="sync"> 仅同步进展</label>
      <button type="submit">发送</button>
    </form></section>
    """


def _lifecycle_forms(case: Any, path_for: Callable[[str], str]) -> str:
    if case.lifecycle_status is LifecycleStatus.OPEN or not case.can_change_consult_status:
        return ""
    reopen_path = path_for(f"/issues/{escape(case.case_ref)}/reopen")
    return f"""
    <form method="post" action="{reopen_path}">
      <input type="hidden" name="version" value="{case.version}">
      <label>重新打开后由谁处理
        <select name="waiting_on"><option value="dev">待研发处理</option>
          <option value="consult">待咨询处理</option></select>
      </label>
      <button type="submit">重新打开</button>
    </form>
    """


def _summary_json(summary: Any) -> dict[str, Any]:
    return {
        "issue_number": summary.issue_number,
        "bug_number": summary.bug_number,
        "title": summary.title,
        "customer_name": summary.customer_name,
        "issue_current_status": summary.issue_current_status,
        "workflow_status": summary.status.value,
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


def _case_card(summary: Any, path_for: Callable[[str], str]) -> str:
    case_ref = escape(summary.case_ref)
    title = escape(summary.title)
    customer_name = escape(summary.customer_name or "未录入")
    human_number = escape(summary.bug_number or "编号待生成")
    updated_at = _format_datetime(summary.updated_at)
    case_path = path_for(f"/issues/{case_ref}")
    can_auto_accept = (
        summary.lifecycle_status is LifecycleStatus.OPEN
        and summary.status
        in {
            CaseStatus.PENDING_CONFIRMATION,
            CaseStatus.WAITING_CUSTOMER,
            CaseStatus.SUSPENDED,
        }
        and (summary.status is not CaseStatus.PENDING_CONFIRMATION or summary.can_accept)
    )
    card_content = (
        f'<div class="case-card-top"><strong class="case-card-title">{title}</strong>'
        f'<div class="case-card-badges">{_priority_badge(summary.priority)}'
        f"{_case_list_status_badge(summary)}"
        f"{_deadline_badge(summary.deadline_status)}</div></div>"
        f'<div class="case-customer">客户 / 企业：{customer_name}</div>'
        f'<div class="case-card-meta"><span class="case-ref">{human_number}</span>'
        f"<span>截止 {_format_datetime(summary.deadline)}</span>"
        f"<span>最近更新 {updated_at}</span></div>"
    )
    if can_auto_accept:
        open_action = path_for(f"/issues/{case_ref}/open")
        return (
            f'<form class="case-card case-card-auto-accept" method="post" action="{open_action}">'
            f'<button type="submit" class="case-card-hitbox" '
            f'aria-label="受理并打开问题 {human_number}"></button>'
            f'<div class="case-card-content">{card_content}</div></form>'
        )
    return (
        '<article class="case-card">'
        f'<a class="case-card-link" href="{case_path}">{card_content}</a></article>'
    )


def _case_list_status_badge(summary: Any) -> str:
    status = escape(summary.issue_current_status or "问题未回复")
    return f'<span class="status-badge issue-current-status">{status}</span>'


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
        "current_dev_team_name": case.current_dev_team_name,
        "current_dev_team_lead_display_name": case.current_dev_team_lead_display_name,
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
