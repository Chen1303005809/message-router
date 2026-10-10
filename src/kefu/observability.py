"""Privacy-preserving identifiers for operational logs."""

from __future__ import annotations

from hashlib import sha256
from os import getpid
from pathlib import Path

from sqlalchemy.engine import URL, make_url


def opaque_id_fingerprint(value: str | None) -> str:
    """Return a stable short fingerprint without logging an opaque identifier."""
    normalized = (value or "").strip()
    if not normalized:
        return "empty"
    return sha256(normalized.encode("utf-8")).hexdigest()[:12]


def database_target_fingerprint(database_url: str | URL) -> str:
    """Fingerprint the shared database target without exposing credentials."""
    url = make_url(database_url) if isinstance(database_url, str) else database_url
    database = url.database or ""
    process_scope = ""
    if url.drivername.startswith("sqlite"):
        is_memory_database = (
            database in {"", ":memory:"}
            or database.startswith("file::memory:")
            or url.query.get("mode") == "memory"
        )
        if is_memory_database:
            # Separate processes never share SQLAlchemy's in-memory SQLite DB.
            process_scope = f"process-local:{getpid()}"
        elif database.startswith("file:"):
            uri_path, separator, uri_query = database[5:].partition("?")
            database = f"file:{Path(uri_path).expanduser().resolve()}"
            if separator:
                database = f"{database}?{uri_query}"
        else:
            database = str(Path(database).expanduser().resolve())
    target = "\x1f".join(
        (
            url.drivername,
            url.host or "",
            str(url.port or ""),
            database,
            url.username or "",
            repr(sorted(url.query.items())),
            process_scope,
        )
    )
    return sha256(target.encode("utf-8")).hexdigest()[:12]
