# ruff: noqa: E501

"""FastAPI application assembly for the H5 JSON API."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from uuid import uuid4

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kefu.admin.service import (
    Administration,
    initialize_global_admin,
)
from kefu.agent.service import (
    Agent,
    build_tool_registry,
)
from kefu.case_desk.contracts import (
    Actor,
)
from kefu.case_desk.errors import (
    CaseDeskError,
)
from kefu.case_desk.service import CaseDesk
from kefu.config import Settings
from kefu.inspection.service import IssueInspectionService
from kefu.issues.service import IssueManagement
from kefu.logging_setup import configure_logging
from kefu.media.storage import (
    MediaIngestor,
    ObjectStorage,
)
from kefu.messages.push import MessagePush
from kefu.messages.receiver import MessageReceiver
from kefu.messages.recipients import RecipientResolver
from kefu.observability import database_target_fingerprint
from kefu.persistence.database import create_engine_from_url, create_session_factory
from kefu.relay.handler import Relay
from kefu.web.api_support import _elapsed_ms, _http_error, _storage_from_settings
from kefu.web.auth import WebAuthenticator
from kefu.web.runtime import WebRuntime
from kefu.wecom.customer_groups import WeComCustomerGroupClient

configure_logging("WEB")
# httpx's built-in INFO log prints full request URLs, including access_token query values.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
_PRIVATE_INTAKE_COOKIE = "kefu_private_intake"


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
            path[len(mount_path) :] if mount_path and path.startswith(f"{mount_path}/") else path
        )
        if internal_path == "/api/events" or internal_path.startswith("/api/events/"):
            new_path = "/api/issues" + internal_path[len("/api/events") :]
            destination = external_path(new_path)
            if request.url.query:
                destination = f"{destination}?{request.url.query}"
            return RedirectResponse(destination, status_code=307)
        if internal_path in {"/api/issues/drafts", "/api/agent/chat"}:
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

    @app.on_event("shutdown")
    async def close_agent_client() -> None:
        await agent.close()

    from kefu.web.routes_admin import register_admin_routes
    from kefu.web.routes_agent import register_agent_routes
    from kefu.web.routes_intake import register_intake_routes
    from kefu.web.routes_issues import register_issues_routes
    from kefu.web.routes_session import register_session_routes

    runtime = WebRuntime(
        settings=settings,
        session_factory=session_factory,
        authenticator=authenticator,
        storage=storage,
        wecom_customer_groups=wecom_customer_groups,
        desk=desk,
        administration=administration,
        media_ingestor=media_ingestor,
        issue_management=issue_management,
        message_push=message_push,
        relay=relay,
        message_receiver=message_receiver,
        recipient_resolver=recipient_resolver,
        inspection=inspection,
        agent_tools=agent_tools,
        agent=agent,
        external_path=external_path,
        actor_or_error=actor_or_error,
    )
    app.state.web_runtime = runtime
    register_session_routes(app, runtime)
    register_intake_routes(app, runtime)
    register_agent_routes(app, runtime)
    register_admin_routes(app, runtime)
    register_issues_routes(app, runtime)
    return app


app = create_app()
