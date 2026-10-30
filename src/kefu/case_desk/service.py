"""Stable facade for CaseDesk domain operations."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from kefu.routing.directory import DatabaseRoutingDirectory

from .service_commands import CaseDeskCommands
from .service_delivery import CaseDeskDelivery
from .service_queries import CaseDeskQueries
from .service_support import DEFAULT_WEB_BASE_URL, utc_now

__all__ = ["CaseDesk", "utc_now"]


class CaseDesk(CaseDeskCommands, CaseDeskQueries, CaseDeskDelivery):
    """Authorized issue operations with stable query, command, and worker APIs."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        directory: DatabaseRoutingDirectory | None = None,
        web_base_url: str = DEFAULT_WEB_BASE_URL,
    ) -> None:
        self._session_factory = session_factory
        self._directory = directory or DatabaseRoutingDirectory()
        self._web_base_url = web_base_url.strip().rstrip("/")
        if not self._web_base_url:
            raise ValueError("web_base_url 不能为空")
