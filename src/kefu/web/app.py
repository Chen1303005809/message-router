# ruff: noqa: E501

"""Server-rendered H5 event center with no business rules of its own."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal, DecimalException
from hmac import compare_digest
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlencode
from uuid import UUID
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
from kefu.case_desk.contracts import (
    Actor,
    CaseFilter,
    CloseCase,
    CreateCase,
    EntryView,
    ExtendCaseDeadline,
    ImagePart,
    PostFormalMessage,
    ReopenCase,
    SetCaseApproachingWindow,
    TextPart,
    TransferConsultant,
    TransferDeveloper,
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
from kefu.media.storage import (
    MAX_IMAGE_BYTES,
    MediaIngestError,
    MediaIngestor,
    ObjectStorage,
    object_storage_from_settings,
)
from kefu.persistence.database import create_engine_from_url, create_session_factory
from kefu.persistence.models import (
    MAX_DEADLINE_INCREMENT_MINUTES,
    MIN_DEADLINE_INCREMENT_MINUTES,
    CaseEntryKind,
    CasePriority,
    DeadlineStatus,
    DeliveryDestination,
    DeliveryStatus,
    EntrySide,
    LifecycleStatus,
    MembershipRole,
    MessageIntent,
    PartKind,
    TeamKind,
    WaitingOn,
)
from kefu.web.auth import AuthenticationError, WebAuthenticator
from kefu.wecom.transport import InboundImagePart


def create_app(
    *,
    session_factory: Callable[[], Session] | None = None,
    settings: Settings | None = None,
    authenticator: WebAuthenticator | None = None,
    storage: ObjectStorage | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if session_factory is None:
        session_factory = create_session_factory(create_engine_from_url(settings.database_url))
    desk = CaseDesk(session_factory, web_base_url=settings.web_base_url)
    administration = Administration(session_factory)
    authenticator = authenticator or WebAuthenticator(session_factory, settings)
    storage = storage or _storage_from_settings(settings)
    if settings.initial_admin_wecom_userid:
        initialize_global_admin(
            session_factory,
            wecom_userid=settings.initial_admin_wecom_userid,
            display_name=settings.initial_admin_display_name,
        )
    app = FastAPI(title="客户问题事件中心", version="0.1.0")

    def external_path(path: str) -> str:
        """Build a browser path that preserves an optional proxy mount prefix."""
        if not path.startswith("/"):
            raise ValueError("H5 路径必须以 / 开头")
        mount_path = settings.web_mount_path
        if not mount_path or path == mount_path or path.startswith(f"{mount_path}/"):
            return path
        return f"{mount_path}{path}"

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
        return RedirectResponse(external_path("/events"), status_code=302)

    @app.get("/auth/login", include_in_schema=False)
    async def login(next: str | None = None) -> RedirectResponse:
        try:
            return authenticator.begin_login(next or external_path("/events"))
        except CaseDeskError as error:
            raise _http_error(error) from error

    @app.get("/auth/callback", include_in_schema=False)
    async def oauth_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
        try:
            return authenticator.complete_login(request, code=code, state=state)
        except CaseDeskError as error:
            raise _http_error(error) from error

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
          <div class="eyebrow">首次使用 · H5 事件管理中心</div>
          <h1>初始化总管理员</h1>
          <p class="muted">总管理员使用企业微信 userid 登录，不单独设置密码。初始化完成后，人员授权、团队配置和研发群绑定都可以在管理中心可视化完成。</p>
          <div class="notice">{escape(token_hint)}</div>
          <form method="post" action="{external_path('/setup')}">
            <label>企业微信 userid
              <input name="wecom_userid" required maxlength="128" placeholder="例如 zhangsan">
            </label>
            <label>显示名称
              <input name="display_name" required maxlength="256" value="事件中心总管理员">
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
                    _payload_text(payload, "display_name")
                    if "display_name" in payload
                    else None
                ),
                active=(
                    _payload_bool(payload, "active") if "active" in payload else None
                ),
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

    @app.get("/events", response_class=HTMLResponse)
    async def events_page(
        request: Request, state: str = "open", priority: str = ""
    ) -> Response:
        if state not in {"mine", "dev", "open", "closed"}:
            state = "open"
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            overview = desk.overview(actor)
            cases = desk.list_cases(_case_filter_for_state(state, priority), actor)
            consult_queues = desk.list_consult_queues(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        cards = "".join(_case_card(summary, external_path) for summary in cases.items)
        cards = cards or '<p class="empty">当前筛选下没有事件。</p>'
        events_path = external_path("/events")
        tabs = " ".join(
            f'<a class="{"active" if state == value else ""}" '
            f'href="{_event_filter_url(events_path, value, priority)}">{label}</a>'
            for value, label in (
                ("mine", "待咨询处理"),
                ("dev", "待研发"),
                ("open", "全部进行中"),
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
        create_link = (
            f'<a class="nav-create" href="{external_path("/events/new")}">＋ 新建事件</a>'
            if consult_queues
            else ""
        )
        overview_cards = "".join(
            f'<article class="overview-card"><span>{label}</span><strong>{count}</strong></article>'
            for label, count in (
                ("全部进行中", overview.open_count),
                ("待咨询处理", overview.waiting_consult_count),
                ("等待研发", overview.waiting_dev_count),
                ("已关闭", overview.closed_count),
            )
        )
        return HTMLResponse(
            _page(
                "事件中心",
                f'<header class="topbar"><div><div class="eyebrow">客户问题 · 全链路跟踪</div>'
                f"<h1>事件工作台</h1><p class=\"muted\">按当前责任方和紧急程度跟进每一条问题</p></div>"
                f'<nav>{tabs}{admin_link}{create_link}</nav></header>'
                f'<section class="overview-grid" aria-label="事件数据一览">{overview_cards}</section>'
                f'<section class="filter-panel"><div class="filter-label">紧急程度</div>'
                f'<nav class="priority-filters">{priority_filters}</nav></section>'
                f'<main class="event-list">{cards}</main>',
                body_class="event-page",
            )
        )

    @app.get("/api/events")
    async def events_api(
        request: Request, state: str = "open", priority: str = ""
    ) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            page = desk.list_cases(_case_filter_for_state(state, priority), actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return JSONResponse(
            {
                "items": [_summary_json(item) for item in page.items],
                "offset": page.offset,
                "next_offset": page.next_offset,
            }
        )

    @app.get("/drafts/{draft_id}/new", response_class=HTMLResponse)
    async def new_case_page(request: Request, draft_id: UUID) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            draft = desk.get_draft(draft_id, actor)
            queues = desk.list_consult_queues(actor)
            developers = desk.list_developers(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        queue_options = "".join(
            f'<option value="{queue.id}">{escape(queue.name)}</option>' for queue in queues
        )
        developer_options = "".join(
            f'<option value="{developer.id}">{escape(developer.display_name)}'
            f"（{escape(developer.wecom_userid)}）</option>"
            for developer in developers
        )
        body = f"""
        <main class="create-main">
          <a class="back-link" href="{external_path('/events')}">← 返回事件中心</a>
          <header class="page-heading"><div class="eyebrow">建立跟踪事件</div>
            <h1>创建新事件</h1><p class="muted">补全客户信息和紧急程度，明确后续责任团队。</p></header>
          <section class="panel source-panel"><h2>客户问题原始消息</h2>
            {_render_parts(draft.parts, None)}</section>
          <form class="panel create-form" method="post">
            <h2>事件信息</h2>
            <label>客户 / 企业名称 <input name="customer_name" required maxlength="256" placeholder="例如：杭州某某科技"></label>
            <div class="form-row">
              <label>联系人（选填） <input name="customer_contact_name" maxlength="256"></label>
              <label>联系方式（选填） <input name="customer_contact_method" maxlength="256" placeholder="电话、邮箱或其他联系方法"></label>
            </div>
            <div class="form-row">
              <label>紧急程度
                <select name="priority">
                  <option value="normal">普通</option><option value="urgent">紧急</option>
                  <option value="severe">严重</option>
                </select>
              </label>
              <label>事件标题 <input name="title" required maxlength="512"></label>
            </div>
            <div class="form-row">
              <label>咨询队列 <select name="consult_queue_id" required>{queue_options}</select></label>
              <label>研发处理人 <select name="developer_id" required>{developer_options}</select></label>
            </div>
            <p class="muted">默认截止时间为创建后 3 小时，临近期限提醒默认提前 24 小时；创建后可由咨询人员调整。</p>
            <button class="primary" type="submit">创建并发送给研发</button>
          </form>
        </main>
        """
        return HTMLResponse(_page("创建事件", body, body_class="case-create-page"))

    @app.post("/drafts/{draft_id}/new", include_in_schema=False)
    async def create_case_from_draft(request: Request, draft_id: UUID) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            result = desk.execute(
                CreateCase(
                    title=_required(form, "title"),
                    consult_queue_id=UUID(_required(form, "consult_queue_id")),
                    developer_id=UUID(_required(form, "developer_id")),
                    customer_name=_required(form, "customer_name"),
                    customer_contact_name=form.get("customer_contact_name"),
                    customer_contact_method=form.get("customer_contact_method"),
                    priority=CasePriority(form.get("priority", CasePriority.NORMAL.value)),
                    draft_id=draft_id,
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        assert result.case_ref is not None
        return RedirectResponse(external_path(f"/events/{result.case_ref}"), status_code=303)

    @app.get("/events/new", response_class=HTMLResponse)
    async def new_case_form_page(request: Request) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            queues = desk.list_consult_queues(actor)
            developers = desk.list_developers(actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        if not queues:
            raise _http_error(Forbidden("你没有可用于创建事件的咨询队列权限"))
        queue_options = "".join(
            f'<option value="{queue.id}">{escape(queue.name)}</option>' for queue in queues
        )
        developer_options = "".join(
            f'<option value="{developer.id}">{escape(developer.display_name)}'
            f"（{escape(developer.wecom_userid)}）</option>"
            for developer in developers
        )
        body = f"""
        <main class="create-main">
          <a class="back-link" href="{external_path('/events')}">← 返回事件中心</a>
          <header class="page-heading"><div class="eyebrow">无需先私聊机器人</div>
            <h1>创建新事件</h1><p class="muted">填写问题和客户信息，补充图片后直接交给研发跟进。</p></header>
          <form class="panel create-form" method="post" action="{external_path('/events/new')}" enctype="multipart/form-data">
            <h2>事件信息</h2>
            <label>事件标题 <input name="title" required maxlength="512" placeholder="例如：订单提交后页面报错"></label>
            <label>问题描述 <textarea name="description" required maxlength="20000" rows="6" placeholder="描述客户现象、发生时间、影响范围和复现步骤"></textarea></label>
            <label>问题图片（可多选，单张不超过 20 MiB）
              <input name="images" type="file" accept="image/*" multiple>
            </label>
            <label>客户 / 企业名称 <input name="customer_name" required maxlength="256" placeholder="例如：杭州某某科技"></label>
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
              <label>咨询队列 <select name="consult_queue_id" required>
                <option value="" disabled selected>选择咨询队列</option>{queue_options}
              </select></label>
            </div>
            <label>研发处理人 <select name="developer_id" required>
              <option value="" disabled selected>选择研发处理人</option>{developer_options}
            </select></label>
            <p class="muted">描述文字必填，图片可选。默认截止时间为创建后 3 小时，临近期限提醒默认提前 24 小时；创建后可在详情中调整。</p>
            <button class="primary" type="submit">创建并发送给研发</button>
          </form>
        </main>
        """
        return HTMLResponse(_page("创建事件", body, body_class="case-create-page"))

    @app.post("/events/new", include_in_schema=False)
    async def create_case_from_h5(request: Request) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await request.form()
        try:
            title = _required_form_text(form, "title")
            description = _required_form_text(form, "description")
            customer_name = _required_form_text(form, "customer_name")
            customer_contact_name = _optional_form_text(form, "customer_contact_name")
            customer_contact_method = _optional_form_text(form, "customer_contact_method")
            consult_queue_id = UUID(_required_form_text(form, "consult_queue_id"))
            developer_id = UUID(_required_form_text(form, "developer_id"))
            priority = CasePriority(_optional_form_text(form, "priority") or "normal")
            if len(title) > 512:
                raise ValidationError("事件标题不能超过 512 个字符")
            if len(description) > 20_000:
                raise ValidationError("问题描述不能超过 20000 个字符")
            if len(customer_name) > 256:
                raise ValidationError("客户 / 企业名称不能超过 256 个字符")
            if customer_contact_name and len(customer_contact_name) > 256:
                raise ValidationError("联系人不能超过 256 个字符")
            if customer_contact_method and len(customer_contact_method) > 256:
                raise ValidationError("联系方式不能超过 256 个字符")

            queues = desk.list_consult_queues(actor)
            if consult_queue_id not in {queue.id for queue in queues}:
                raise Forbidden("你不属于所选咨询队列")
            developers = desk.list_developers(actor)
            if developer_id not in {developer.id for developer in developers}:
                raise Forbidden("所选研发处理人不可用")

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

            result = desk.execute(
                CreateCase(
                    title=title,
                    consult_queue_id=consult_queue_id,
                    developer_id=developer_id,
                    customer_name=customer_name,
                    customer_contact_name=customer_contact_name,
                    customer_contact_method=customer_contact_method,
                    priority=priority,
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
        assert result.case_ref is not None
        return RedirectResponse(external_path(f"/events/{result.case_ref}"), status_code=303)

    @app.get("/events/{case_ref}", response_class=HTMLResponse)
    async def event_detail(request: Request, case_ref: str) -> Response:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        try:
            case = desk.get_case(case_ref, actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        can_use_side_selector = administration.is_global_admin(actor)
        deliveries_by_entry: dict[UUID, list[Any]] = {}
        for delivery in case.deliveries:
            deliveries_by_entry.setdefault(delivery.entry_id, []).append(delivery)
        entries = "".join(
            _render_entry(
                case.case_ref,
                entry,
                external_path,
                deliveries_by_entry.get(entry.id, ()),
            )
            for entry in case.entries
        )
        events_path = external_path("/events")
        body = f"""
        <main class="case-main">
          <a class="back-link" href="{events_path}">← 返回事件工作台</a>
          <header class="case-titlebar">
            <div><div class="eyebrow">事件编号 〔KF·{escape(case.case_ref)}〕</div>
              <h1>{escape(case.title)}</h1></div>
            <div class="case-state-badges">
              {_lifecycle_badge(case.lifecycle_status)}
              {_waiting_badge(case.waiting_on)}
              {_deadline_badge(case.deadline_status)}
              {_priority_badge(case.priority)}
            </div>
          </header>
          <section class="case-overview panel">
            <div class="case-overview-heading"><h2>客户与责任信息</h2>
              <div class="updated-at">最近更新 { _format_datetime(case.updated_at) }</div></div>
            <div class="case-meta-grid">
              <div><span>客户 / 企业</span><strong>{escape(case.customer_name or '未录入')}</strong></div>
              <div><span>联系人</span><strong>{escape(case.customer_contact_name or '未填写')}</strong></div>
              <div><span>联系方式</span><strong>{escape(case.customer_contact_method or '未填写')}</strong></div>
              <div><span>所属咨询队列</span><strong>{escape(case.consult_queue_name)}</strong></div>
              <div><span>当前咨询经办人</span><strong>{escape(case.current_consultant_name or '未指定')}</strong></div>
              <div><span>当前研发责任团队</span><strong>{escape(case.current_dev_team_name)}</strong></div>
              <div><span>研发处理人</span><strong>{escape(case.current_developer_name or '未指定')}</strong></div>
              <div><span>创建时间</span><strong>{_format_datetime(case.created_at)}</strong></div>
              <div><span>截止时间</span><strong>{_format_datetime(case.deadline)}</strong></div>
              <div><span>临近期限提醒</span><strong>提前 {_duration_label(case.approaching_window_minutes)}</strong></div>
            </div>
          </section>
          {_deadline_controls(case, external_path)}
          {_metadata_form(case, external_path)}
          <section class="timeline-section">
            <div class="section-heading"><h2>事件时间线</h2><span>{len(case.entries)} 条记录</span></div>
            <div class="timeline">{entries}</div>
          </section>
          <section class="action-panel panel">
            {_message_form(case.case_ref, case.version, case.lifecycle_status, external_path, show_side=can_use_side_selector)}
            {_lifecycle_forms(case.case_ref, case.version, case.lifecycle_status, external_path)}
          </section>
        </main>
        """
        return HTMLResponse(_page(case.title, body, body_class="case-detail-page"))

    @app.get("/api/events/{case_ref}")
    async def event_api(request: Request, case_ref: str) -> JSONResponse:
        actor = actor_or_error(request)
        try:
            case = desk.get_case(case_ref, actor)
        except CaseDeskError as error:
            raise _http_error(error) from error
        return JSONResponse(_case_json(case))

    @app.post("/events/{case_ref}/messages", include_in_schema=False)
    async def post_message(request: Request, case_ref: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            intent = MessageIntent(form.get("intent", MessageIntent.HANDOFF.value))
            side = EntrySide(form["side"]) if form.get("side") else None
            desk.execute(
                PostFormalMessage(
                    case_ref=case_ref,
                    expected_version=int(_required(form, "version")),
                    parts=(TextPart(_required(form, "text")),),
                    intent=intent,
                    side=side,
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/events/{escape(case_ref)}"), status_code=303)

    @app.post("/events/{case_ref}/metadata", include_in_schema=False)
    async def update_event_metadata(request: Request, case_ref: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            desk.execute(
                UpdateCaseMetadata(
                    case_ref=case_ref,
                    expected_version=int(_required(form, "version")),
                    customer_name=_required(form, "customer_name"),
                    customer_contact_name=form.get("customer_contact_name"),
                    customer_contact_method=form.get("customer_contact_method"),
                    priority=CasePriority(_required(form, "priority")),
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/events/{escape(case_ref)}"), status_code=303)

    @app.post("/events/{case_ref}/deadline/extend", include_in_schema=False)
    async def extend_event_deadline(request: Request, case_ref: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            desk.execute(
                ExtendCaseDeadline(
                    case_ref=case_ref,
                    expected_version=int(_required(form, "version")),
                    extension_minutes=_half_hour_hours_to_minutes(
                        _required(form, "extension_hours"), "延长期限"
                    ),
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/events/{escape(case_ref)}"), status_code=303)

    @app.post("/events/{case_ref}/deadline/approaching-window", include_in_schema=False)
    async def set_event_approaching_window(
        request: Request, case_ref: str
    ) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            desk.execute(
                SetCaseApproachingWindow(
                    case_ref=case_ref,
                    expected_version=int(_required(form, "version")),
                    approaching_window_minutes=_half_hour_hours_to_minutes(
                        _required(form, "approaching_hours"), "临近期限提醒时间"
                    ),
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/events/{escape(case_ref)}"), status_code=303)

    @app.post("/events/{case_ref}/close", include_in_schema=False)
    async def close_event(request: Request, case_ref: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            desk.execute(CloseCase(case_ref, int(_required(form, "version"))), actor)
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/events/{escape(case_ref)}"), status_code=303)

    @app.post("/events/{case_ref}/reopen", include_in_schema=False)
    async def reopen_event(request: Request, case_ref: str) -> RedirectResponse:
        actor = page_actor_or_login(request)
        if isinstance(actor, RedirectResponse):
            return actor
        form = await _urlencoded_form(request)
        try:
            desk.execute(
                ReopenCase(
                    case_ref=case_ref,
                    expected_version=int(_required(form, "version")),
                    waiting_on=WaitingOn(_required(form, "waiting_on")),
                ),
                actor,
            )
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return RedirectResponse(external_path(f"/events/{escape(case_ref)}"), status_code=303)

    @app.post("/api/events/{case_ref}/transfer-consultant")
    async def transfer_consultant(request: Request, case_ref: str) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            result = desk.execute(
                TransferConsultant(
                    case_ref=case_ref,
                    expected_version=int(payload["version"]),
                    new_consultant_id=UUID(str(payload["new_consultant_id"])),
                ),
                actor,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"case_ref": result.case_ref, "version": result.case_version})

    @app.post("/api/events/{case_ref}/transfer-developer")
    async def transfer_developer(request: Request, case_ref: str) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            result = desk.execute(
                TransferDeveloper(
                    case_ref=case_ref,
                    expected_version=int(payload["version"]),
                    new_developer_id=UUID(str(payload["new_developer_id"])),
                ),
                actor,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"case_ref": result.case_ref, "version": result.case_version})

    @app.post("/api/events/{case_ref}/transfer-dev-team")
    async def transfer_dev_team(request: Request, case_ref: str) -> JSONResponse:
        actor = actor_or_error(request)
        payload = await _json_object(request)
        try:
            developer_id = payload.get("new_developer_id")
            result = desk.execute(
                TransferDevTeam(
                    case_ref=case_ref,
                    expected_version=int(payload["version"]),
                    new_dev_team_id=UUID(str(payload["new_dev_team_id"])),
                    new_developer_id=UUID(str(developer_id)) if developer_id else None,
                ),
                actor,
            )
        except (CaseDeskError, ValueError, KeyError) as error:
            raise _http_error(_as_case_desk_error(error)) from error
        return JSONResponse({"case_ref": result.case_ref, "version": result.case_version})

    @app.get("/events/{case_ref}/media/{media_id}", include_in_schema=False)
    async def event_media(request: Request, case_ref: str, media_id: UUID) -> Response:
        actor = actor_or_error(request)
        try:
            access = desk.get_media_access(case_ref, media_id, actor)
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
    if not header_userid or not wecom_userid.strip() or not compare_digest(
        header_userid, wecom_userid.strip()
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
    events_path = path_for("/events")
    visible_users = tuple(user for user in member_options if user.active)
    initialized_label = (
        f"{overview.initialized_dev_team_count}/{overview.dev_team_count} 个研发群已绑定"
        if overview.dev_team_count
        else "暂未创建研发团队"
    )
    search_value = escape(query)

    user_rows = "".join(
        _admin_user_row(user, path_for=path_for) for user in users
    ) or '<p class="empty">没有匹配的成员。</p>'
    team_cards = "".join(
        _admin_team_card(team, users=visible_users, path_for=path_for) for team in teams
    ) or '<p class="empty">还没有团队，请先创建咨询队列或研发责任团队。</p>'
    return f"""
    <header class="topbar admin-topbar">
      <div>
        <div class="eyebrow">H5 事件管理中心</div>
        <h1>组织与授权</h1>
        <p class="muted">总管理员：{escape(actor.user_id.hex[:8])} · 统一管理咨询和研发经办人</p>
      </div>
      <nav><a href="{events_path}">返回事件中心</a><a class="active" href="{admin_path}">管理设置</a></nav>
    </header>
    <main class="admin-main">
      <section class="hero-banner">
        <div><span class="status-dot"></span><strong>权限配置已启用</strong>
          <p>先录入企业微信成员，再将成员加入咨询队列或研发责任团队；角色为“管理员”的成员可以处理本团队的交接和群绑定。</p>
        </div>
        <span class="badge badge-blue">总管理员</span>
      </section>

      <section class="stats-grid">
        <div class="stat-card"><span>在岗成员</span><strong>{overview.active_user_count}</strong><small>共 {overview.user_count} 个身份</small></div>
        <div class="stat-card"><span>咨询队列</span><strong>{overview.consult_queue_count}</strong><small>按队列隔离可见范围</small></div>
        <div class="stat-card"><span>研发团队</span><strong>{overview.dev_team_count}</strong><small>{escape(initialized_label)}</small></div>
        <div class="stat-card"><span>总管理员</span><strong>{overview.global_admin_count}</strong><small>至少保留 1 名在岗管理员</small></div>
      </section>

      <section class="admin-grid">
        <div class="panel" id="users">
          <div class="panel-heading"><div><div class="eyebrow">Step 1</div><h2>成员与总权限</h2></div><span class="count-pill">{overview.user_count}</span></div>
          <p class="panel-intro">用企业微信 userid 建立登录身份。成员加入团队后，才能成为咨询经办人或研发处理人。</p>
          <form class="search-form" method="get" action="{admin_path}">
            <input name="q" value="{search_value}" placeholder="搜索姓名或 userid">
            <button type="submit">搜索</button>
            {f'<a class="button-link" href="{admin_path}">清除</a>' if query else ''}
          </form>
          <details class="add-box" open>
            <summary>＋ 新增企业成员</summary>
            <form method="post" action="{path_for('/admin/users')}" class="form-grid">
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
          <p class="panel-intro">咨询队列负责咨询侧可见范围；研发责任团队负责群路由和研发处理人授权。</p>
          <details class="add-box" open>
            <summary>＋ 新增团队</summary>
            <form method="post" action="{path_for('/admin/teams')}" class="form-grid team-create-form">
              <label>团队类型<select name="kind"><option value="consult_queue">咨询队列</option><option value="dev">研发责任团队</option></select></label>
              <label>团队名称<input name="name" required maxlength="256" placeholder="例如 平台研发组"></label>
              <button class="primary" type="submit">创建团队</button>
            </form>
          </details>
          <div class="managed-list">{team_cards}</div>
        </div>
      </section>

      <section class="permission-note">
        <strong>授权规则</strong>
        <span>成员 = 可查看和处理团队事件；团队管理员 = 可在本团队内执行交接、转交和研发群绑定；总管理员 = 可管理全组织并查看全部事件。</span>
      </section>
    </main>
    """


def _admin_user_row(user: ManagedUser, *, path_for: Callable[[str], str]) -> str:
    user_id = escape(str(user.id))
    memberships = "".join(
        f'<span class="tag {"tag-muted" if not membership.active else ""}">'
        f"{escape(_team_kind_label(membership.team_kind))} · {escape(membership.team_name)} · "
        f"{escape(_role_label(membership.role))}</span>"
        for membership in user.memberships
    ) or '<span class="muted">尚未加入团队</span>'
    global_badge = '<span class="badge badge-amber">总管理员</span>' if user.is_global_admin else ""
    active_badge = '<span class="badge badge-green">在岗</span>' if user.active else '<span class="badge badge-gray">已停用</span>'
    checked_active = " checked" if user.active else ""
    checked_admin = " checked" if user.is_global_admin else ""
    return f"""
    <details class="managed-row">
      <summary><span class="avatar">{escape(user.display_name[:1])}</span><span class="user-summary"><strong>{escape(user.display_name)}</strong><small>{escape(user.wecom_userid)}</small></span>{active_badge}{global_badge}<span class="chevron">⌄</span></summary>
      <div class="managed-detail">
        <div class="tag-list">{memberships}</div>
        <form method="post" action="{path_for(f'/admin/users/{user_id}')}" class="edit-form">
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
    status = '<span class="badge badge-green">启用</span>' if team.active else '<span class="badge badge-gray">已停用</span>'
    checked = " checked" if team.active else ""
    member_rows = "".join(
        _admin_team_member_row(team, member, path_for=path_for) for member in team.members
    ) or '<p class="empty">还没有授权成员。</p>'
    options = "".join(
        f'<option value="{escape(str(user.id))}">{escape(user.display_name)}（{escape(user.wecom_userid)}）</option>'
        for user in users
    ) or '<option value="">请先添加在岗成员</option>'
    channel = ""
    if team.kind is TeamKind.DEV:
        channel_value = escape(team.channel_chatid or "")
        channel_status = (
            f'<span class="muted">当前群：{channel_value}</span>'
            if team.channel_initialized
            else '<span class="warning-text">尚未绑定研发群</span>'
        )
        channel = f"""
        <div class="channel-box"><div><strong>研发群通道</strong>{channel_status}</div>
          <form method="post" action="{path_for(f'/admin/teams/{team_id}/channel')}" class="inline-form">
            <input name="chatid" required maxlength="256" value="{channel_value}" placeholder="粘贴企业微信群 chatid">
            <button type="submit">绑定</button>
          </form>
        </div>
        """
    return f"""
    <article class="team-card">
      <div class="team-header"><div><span class="eyebrow">{escape(team_badge)}</span><h3>{escape(team.name)}</h3></div>{status}</div>
      <form method="post" action="{path_for(f'/admin/teams/{team_id}')}" class="team-edit-form">
        <input name="name" required maxlength="256" value="{escape(team.name)}">
        <label class="check-label"><input type="checkbox" name="active"{checked}> 启用</label>
        <button class="subtle-button" type="submit">保存</button>
      </form>
      {channel}
      <div class="member-heading"><strong>授权成员</strong><span>{sum(1 for member in team.members if member.active)} 人在岗</span></div>
      <div class="member-list">{member_rows}</div>
      <form method="post" action="{path_for(f'/admin/teams/{team_id}/members')}" class="add-member-form">
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
    active = '<span class="status-dot small"></span>' if member.active else '<span class="status-dot small off"></span>'
    role = _role_label(member.role)
    return f"""
    <div class="member-row"><div>{active}<span><strong>{escape(member.display_name)}</strong><small>{escape(member.wecom_userid)} · {escape(role)}</small></span></div>
      <div class="member-actions">
        <form method="post" action="{path_for(f'/admin/teams/{team_id}/members')}" class="role-form">
          <input type="hidden" name="user_id" value="{user_id}">
          <select name="role"><option value="member"{' selected' if member.role is MembershipRole.MEMBER else ''}>成员</option><option value="admin"{' selected' if member.role is MembershipRole.ADMIN else ''}>管理员</option></select>
          <button class="subtle-button" type="submit">保存</button>
        </form>
        <form method="post" action="{path_for(f'/admin/teams/{team_id}/members/{user_id}/remove')}"><button class="danger-button" type="submit">移除</button></form>
      </div>
    </div>
    """


def _team_kind_label(kind: TeamKind) -> str:
    return "咨询队列" if kind is TeamKind.CONSULT_QUEUE else "研发责任团队"


def _role_label(role: MembershipRole) -> str:
    return "团队管理员" if role is MembershipRole.ADMIN else "成员"


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
    return HTTPException(status_code=status, detail=str(error))


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
.status-closed, .waiting-none { color: #58667b; background: #edf0f4; }
.waiting-consult { color: #155eef; background: #eaf1ff; }
.waiting-dev { color: #067647; background: #e5f7ef; }
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
.case-titlebar h1 { margin: 5px 0 0; font-size: 27px; line-height: 1.25; }
.case-state-badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.case-overview { padding: 18px 20px; }
.case-overview-heading, .section-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.case-overview-heading h2, .section-heading h2 { margin-bottom: 13px; font-size: 17px; }
.updated-at, .section-heading span { color: var(--muted); font-size: 12px; }
.case-meta-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 15px 20px; }
.case-meta-grid div { min-width: 0; }
.case-meta-grid span { display: block; color: var(--muted); font-size: 11px; margin-bottom: 3px; }
.case-meta-grid strong { display: block; overflow-wrap: anywhere; font-size: 13px; }
.deadline-controls { margin-top: 12px; padding: 18px 20px; }
.deadline-controls h2 { margin: 0 0 12px; font-size: 16px; }
.deadline-control-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.deadline-control-grid form { min-width: 0; margin: 0; }
.deadline-control-grid p { margin: 5px 0 10px; font-size: 12px; }
.metadata-editor { margin-top: 12px; padding: 0 16px; }
.metadata-editor summary { padding: 13px 0; cursor: pointer; color: var(--blue); font-size: 13px; font-weight: 700; }
.metadata-editor form { border-top: 1px solid var(--line); padding: 6px 0 14px; }
.metadata-editor[open] summary { border-bottom: 1px solid var(--line); }
.form-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.create-main { max-width: 760px; margin: 0 auto; }
.source-panel, .create-form { padding: 18px 20px; margin: 12px 0; }
.source-panel h2, .create-form h2 { font-size: 16px; margin-bottom: 13px; }
.create-form label, .metadata-editor label { font-size: 13px; }
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
  body.event-page, body.case-detail-page, body.case-create-page { padding: 18px 14px 34px; }
  .topbar { align-items: flex-start; flex-direction: column; gap: 10px; }
  .topbar nav { justify-content: flex-start; margin: 0; }
  .overview-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
  .overview-card { padding: 12px; }
  .case-titlebar { flex-direction: column; }
  .case-state-badges { justify-content: flex-start; }
  .case-meta-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .deadline-control-grid { grid-template-columns: 1fr; }
  .form-row { grid-template-columns: 1fr; gap: 0; }
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
  .case-overview, .source-panel, .create-form, .action-panel, .deadline-controls { padding: 15px; }
  .entry { padding: 11px 12px; }
  .entry-heading { flex-direction: column; gap: 2px; }
  .entry-heading time { font-size: 10px; }
  .case-card-meta { align-items: flex-start; flex-direction: column; }
}
"""


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


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")


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


def _lifecycle_badge(lifecycle: LifecycleStatus) -> str:
    if lifecycle is LifecycleStatus.CLOSED:
        label, color = "已关闭", "status-closed"
    else:
        label, color = "进行中", "status-open"
    return f'<span class="status-badge {color}">{label}</span>'


def _waiting_badge(waiting: WaitingOn) -> str:
    label, color = {
        WaitingOn.CONSULT: ("待咨询", "waiting-consult"),
        WaitingOn.DEV: ("待研发", "waiting-dev"),
        WaitingOn.NONE: ("无待处理动作", "waiting-none"),
    }[waiting]
    return f'<span class="status-badge {color}">{label}</span>'


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
        CaseEntryKind.TRANSFER_CONSULTANT: "咨询经办人变更",
        CaseEntryKind.TRANSFER_DEVELOPER: "研发处理人变更",
        CaseEntryKind.TRANSFER_DEV_TEAM: "研发团队变更",
        CaseEntryKind.CLOSED: "事件关闭",
        CaseEntryKind.REOPENED: "事件重新打开",
        CaseEntryKind.CORRECTION: "消息纠错",
        CaseEntryKind.CASE_METADATA_UPDATED: "事件资料变更",
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
                descriptions.append(
                    f"{labels.get(str(key), str(key))}：{before} → {after}"
                )
        return "；".join(descriptions) or "事件资料已更新"
    if entry.kind is CaseEntryKind.TRANSFER_CONSULTANT:
        return (
            f"咨询经办人由 {metadata.get('from_consultant') or '未指定'} "
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
            f"研发责任团队由 {from_team} 调整为 {to_team}；"
            f"研发处理人由 {from_developer} 调整为 {to_developer}"
        )
    if entry.kind is CaseEntryKind.CLOSED:
        return "咨询侧已确认客户问题闭环"
    if entry.kind is CaseEntryKind.REOPENED:
        waiting = metadata.get("waiting_on")
        next_step = "研发" if waiting == WaitingOn.DEV.value else "咨询"
        return f"事件重新打开，下一步由{next_step}处理"
    if entry.kind is CaseEntryKind.CORRECTION:
        target = metadata.get("corrected_to_case_ref") or metadata.get("corrected_from_case_ref")
        return f"消息关联已更正{f'，目标事件 {target}' if target else ''}"
    return ""


def _priority_label_value(value: str) -> str:
    return {
        CasePriority.NORMAL.value: "普通",
        CasePriority.URGENT.value: "紧急",
        CasePriority.SEVERE.value: "严重",
    }.get(value, value)


def _render_delivery_badges(deliveries: tuple[Any, ...] | list[Any]) -> str:
    if not deliveries:
        return ""
    chips: list[str] = []
    for delivery in deliveries:
        target = "研发群" if delivery.destination_type is DeliveryDestination.CHAT else "咨询侧"
        label, color = {
            DeliveryStatus.PENDING: ("投递中", "delivery-pending"),
            DeliveryStatus.SENT: ("已送达", "delivery-sent"),
            DeliveryStatus.FAILED: ("投递失败", "delivery-failed"),
        }[delivery.status]
        chips.append(
            f'<span class="delivery-badge {color}">{target} · {label}</span>'
        )
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
        EntrySide.SYSTEM: "事件记录",
    }[entry.side]
    heading = (
        f'<div class="entry-author"><strong>{escape(entry.actor_name_snapshot)}</strong>'
        f'<span>{side_label} · {_entry_kind_label(entry.kind)}</span></div>'
    )
    description = _entry_description(entry)
    detail = (
        f'<p class="entry-description">{escape(description)}</p>' if description else ""
    )
    intent_badge = (
        '<span class="entry-intent">仅同步</span>'
        if entry.message_intent is MessageIntent.SYNC
        else ""
    )
    delivery_badges = _render_delivery_badges(deliveries)
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
            media_path = path_for(f"/events/{escape(case_ref)}/media/{part.media_id}")
            rendered.append(
                f'<img alt="事件图片" src="{media_path}">'
            )
        else:
            rendered.append('<p class="part">[图片]</p>')
    return "".join(rendered)


def _metadata_form(case: Any, path_for: Callable[[str], str]) -> str:
    if not case.can_edit_metadata:
        return ""
    action = path_for(f"/events/{escape(case.case_ref)}/metadata")
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
            <input name="customer_name" required maxlength="256" value="{escape(case.customer_name or '')}">
          </label>
          <label>联系人（选填）
            <input name="customer_contact_name" maxlength="256" value="{escape(case.customer_contact_name or '')}">
          </label>
        </div>
        <div class="form-row">
          <label>联系方式（选填）
            <input name="customer_contact_method" maxlength="256" value="{escape(case.customer_contact_method or '')}">
          </label>
          <label>紧急程度 <select name="priority">{priority_options}</select></label>
        </div>
        <button class="primary" type="submit">保存变更</button>
      </form>
    </details>
    """


def _deadline_controls(case: Any, path_for: Callable[[str], str]) -> str:
    if not case.can_extend_deadline or case.lifecycle_status is LifecycleStatus.CLOSED:
        return ""
    extend_action = path_for(f"/events/{escape(case.case_ref)}/deadline/extend")
    approaching_action = path_for(f"/events/{escape(case.case_ref)}/deadline/approaching-window")
    approaching_hours = _hours_value(case.approaching_window_minutes)
    return f"""
    <section class="deadline-controls panel">
      <h2>期限管理</h2>
      <div class="deadline-control-grid">
        <form method="post" action="{extend_action}">
          <input type="hidden" name="version" value="{case.version}">
          <label>延长时长（小时）
            <input type="number" name="extension_hours" min="0.5" step="0.5" value="0.5" required>
          </label>
          <p class="muted">在当前截止时间基础上延长，按至少 30 分钟递增。</p>
          <button class="primary" type="submit">延长截止时间</button>
        </form>
        <form method="post" action="{approaching_action}">
          <input type="hidden" name="version" value="{case.version}">
          <label>提前多久进入临近期限（小时）
            <input type="number" name="approaching_hours" min="0.5" step="0.5" value="{approaching_hours}" required>
          </label>
          <p class="muted">默认提前 24 小时，按至少 30 分钟递增。</p>
          <button class="primary" type="submit">保存临近期限设置</button>
        </form>
      </div>
    </section>
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
    message_path = path_for(f"/events/{escape(case_ref)}/messages")
    side_selector = (
        '<label>发送身份<select name="side">'
        '<option value="consult">咨询</option><option value="dev">研发</option>'
        '</select></label>'
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


def _lifecycle_forms(
    case_ref: str, version: int, lifecycle: LifecycleStatus, path_for: Callable[[str], str]
) -> str:
    if lifecycle is LifecycleStatus.OPEN:
        close_path = path_for(f"/events/{escape(case_ref)}/close")
        return f"""
        <form method="post" action="{close_path}">
          <input type="hidden" name="version" value="{version}">
          <button type="submit">确认客户侧闭环并关闭</button>
        </form>
        """
    reopen_path = path_for(f"/events/{escape(case_ref)}/reopen")
    return f"""
    <form method="post" action="{reopen_path}">
      <input type="hidden" name="version" value="{version}">
      <label>重新打开后等待
        <select name="waiting_on"><option value="dev">研发</option>
          <option value="consult">咨询</option></select>
      </label>
      <button type="submit">重新打开</button>
    </form>
    """


def _summary_json(summary: Any) -> dict[str, Any]:
    return {
        "case_ref": summary.case_ref,
        "title": summary.title,
        "customer_name": summary.customer_name,
        "priority": summary.priority.value,
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
    updated_at = _format_datetime(summary.updated_at)
    case_path = path_for(f"/events/{case_ref}")
    return (
        f'<a class="case-card" href="{case_path}">'
        f'<div class="case-card-top"><strong class="case-card-title">{title}</strong>'
        f'<div class="case-card-badges">{_priority_badge(summary.priority)}'
        f"{_lifecycle_badge(summary.lifecycle_status)}{_waiting_badge(summary.waiting_on)}"
        f"{_deadline_badge(summary.deadline_status)}</div></div>"
        f'<div class="case-customer">客户 / 企业：{customer_name}</div>'
        f'<div class="case-card-meta"><span class="case-ref">〔KF·{case_ref}〕</span>'
        f"<span>截止 {_format_datetime(summary.deadline)}</span>"
        f"<span>最近更新 {updated_at}</span></div></a>"
    )


def _case_json(case: Any) -> dict[str, Any]:
    deliveries_by_entry: dict[UUID, list[Any]] = {}
    for delivery in case.deliveries:
        deliveries_by_entry.setdefault(delivery.entry_id, []).append(delivery)
    return {
        "case_ref": case.case_ref,
        "title": case.title,
        "customer_name": case.customer_name,
        "customer_contact_name": case.customer_contact_name,
        "customer_contact_method": case.customer_contact_method,
        "priority": case.priority.value,
        "lifecycle_status": case.lifecycle_status.value,
        "waiting_on": case.waiting_on.value,
        "deadline": case.deadline.isoformat(),
        "approaching_window_minutes": case.approaching_window_minutes,
        "deadline_status": case.deadline_status.value if case.deadline_status else None,
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
        "consult_queue_name": case.consult_queue_name,
        "current_consultant_name": case.current_consultant_name,
        "current_dev_team_name": case.current_dev_team_name,
        "current_developer_name": case.current_developer_name,
        "can_edit_metadata": case.can_edit_metadata,
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
