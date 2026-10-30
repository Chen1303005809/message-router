"""Domain routes for the H5 JSON API."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    FastAPI,
    Request,
)
from fastapi.responses import RedirectResponse

from kefu.admin.service import (
    initialize_global_admin,
)
from kefu.case_desk.contracts import (
    ImagePart,
)
from kefu.case_desk.errors import (
    CaseDeskError,
)
from kefu.persistence.models import (
    User,
)
from kefu.web.api_models import (
    IntakeOptionsResponse,
    SessionResponse,
    SetupRequest,
    SetupResponse,
    SetupStatusResponse,
)
from kefu.web.runtime import WebRuntime

from .api_support import (
    _PRIVATE_INTAKE_COOKIE,
    _as_case_desk_error,
    _authorize_initial_setup,
    _http_error,
)

logger = logging.getLogger(__name__)


def register_session_routes(app: FastAPI, runtime: WebRuntime) -> None:
    router = APIRouter()
    settings = runtime.settings
    session_factory = runtime.session_factory
    authenticator = runtime.authenticator
    wecom_customer_groups = runtime.wecom_customer_groups
    administration = runtime.administration
    issue_management = runtime.issue_management
    inspection = runtime.inspection
    external_path = runtime.external_path
    actor_or_error = runtime.actor_or_error

    @router.get("/api/session", response_model=SessionResponse)
    async def session_api(request: Request) -> SessionResponse:
        try:
            actor = authenticator.actor_for_request(request)
            setup_required = not administration.has_global_admin()
            if actor is None:
                return SessionResponse(
                    authenticated=False,
                    setup_required=setup_required,
                    auth_mode=settings.auth_mode,
                )
            with session_factory() as session:
                user = session.get(User, actor.user_id)
            return SessionResponse(
                authenticated=user is not None,
                setup_required=setup_required,
                auth_mode=settings.auth_mode,
                user_id=actor.user_id if user is not None else None,
                display_name=user.display_name if user is not None else None,
                is_global_admin=bool(user and user.is_global_admin and user.active),
                can_inspect=inspection.access_for_actor(actor).can_inspect if user else False,
            )
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.get("/api/intake/options", response_model=IntakeOptionsResponse)
    async def intake_options_api(request: Request) -> IntakeOptionsResponse:
        actor = actor_or_error(request)
        try:
            queues = issue_management.list_consult_queues(actor)
            teams = issue_management.list_dev_teams(actor)
            private_intake = None
            token = request.cookies.get(_PRIVATE_INTAKE_COOKIE)
            if token:
                handoff = issue_management.get_private_intake_handoff(token, actor)
                private_intake = {
                    "raw_text": handoff.raw_text,
                    "media": [
                        {"media_id": part.media_id}
                        for part in handoff.parts
                        if isinstance(part, ImagePart)
                    ],
                    "expires_at": handoff.expires_at,
                }
            issue_management.cleanup_expired_drafts()
            return IntakeOptionsResponse(
                consult_queues=[{"id": item.id, "name": item.name} for item in queues],
                dev_teams=[{"id": item.id, "name": item.name} for item in teams],
                default_deadline=datetime.now(ZoneInfo("Asia/Shanghai"))
                + timedelta(hours=settings.issue_default_expiry_hours),
                source_group_lookup_enabled=wecom_customer_groups.is_configured,
                private_intake=private_intake,
            )
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.get("/api/setup", response_model=SetupStatusResponse)
    async def setup_status_api() -> SetupStatusResponse:
        return SetupStatusResponse(required=not administration.has_global_admin())

    @router.post("/api/setup", response_model=SetupResponse)
    async def setup_api(request: Request, payload: SetupRequest) -> SetupResponse:
        try:
            if administration.has_global_admin():
                return SetupResponse(initialized=True)
            _authorize_initial_setup(
                request,
                payload.token,
                payload.wecom_userid,
                settings,
            )
            initialize_global_admin(
                session_factory,
                wecom_userid=payload.wecom_userid,
                display_name=payload.display_name,
            )
            return SetupResponse(initialized=True)
        except (CaseDeskError, ValueError) as error:
            raise _http_error(_as_case_desk_error(error)) from error

    @router.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/auth/login", include_in_schema=False)
    async def login(next: str | None = None) -> RedirectResponse:
        try:
            return authenticator.begin_login(next or external_path("/issues/new"))
        except CaseDeskError as error:
            raise _http_error(error) from error

    @router.get("/auth/callback", include_in_schema=False)
    async def oauth_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
        try:
            return authenticator.complete_login(request, code=code, state=state)
        except CaseDeskError as error:
            raise _http_error(error) from error

    app.include_router(router)
