"""Database construction kept outside of domain code."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from kefu.persistence.models import Base


def create_engine_from_url(database_url: str, *, echo: bool = False) -> Engine:
    """Build the application engine without leaking it into domain interfaces."""
    return create_engine(database_url, echo=echo, future=True, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> Callable[[], Session]:
    """Return independent sessions whose values remain usable after commit."""
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_all(engine: Engine) -> None:
    """Convenience for local development and isolated tests.

    Production schema changes run through Alembic; this helper deliberately has
    no role in service startup.
    """
    Base.metadata.create_all(engine)
