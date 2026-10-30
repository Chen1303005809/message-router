"""Domain routes for the H5 JSON API."""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    Request,
)

from kefu.agent.service import (
    AgentConfigurationError,
)
from kefu.web.api_models import AgentChatRequest, AgentChatResponse
from kefu.web.runtime import WebRuntime

logger = logging.getLogger(__name__)


def register_agent_routes(app: FastAPI, runtime: WebRuntime) -> None:
    router = APIRouter()
    agent = runtime.agent
    actor_or_error = runtime.actor_or_error

    @router.post("/api/agent/chat", response_model=AgentChatResponse)
    async def agent_chat_api(request: Request, payload: AgentChatRequest) -> AgentChatResponse:
        actor = actor_or_error(request)
        try:
            reply = await agent.chat(
                payload.messages,
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
        return AgentChatResponse(message=reply)

    app.include_router(router)
