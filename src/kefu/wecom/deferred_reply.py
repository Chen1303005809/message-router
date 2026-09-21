"""Database-backed correlation for delayed passive replies.

The callback ``req_id`` is only useful together with the original callback
frame. The store therefore persists that frame with a short TTL instead of
keeping it in a worker-local dictionary. A claim lease prevents two worker
processes from releasing the same callback at the same time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from kefu.persistence.models import DeferredPassiveReply
from kefu.wecom.transport import InboundEvent

DEFAULT_CLAIM_LEASE_SECONDS = 60.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PendingPassiveReply:
    """A claimed callback context ready to be answered once."""

    __slots__ = ("id", "case_ref", "req_id", "event", "expires_at", "claim_token")

    def __init__(
        self,
        *,
        id: UUID,
        case_ref: str | None,
        req_id: str,
        event: InboundEvent,
        expires_at: datetime,
        claim_token: UUID | None,
    ) -> None:
        self.id = id
        self.case_ref = case_ref
        self.req_id = req_id
        self.event = event
        self.expires_at = expires_at
        self.claim_token = claim_token


class DeferredPassiveReplyStore:
    """Persist callback contexts so delayed replies survive worker restarts."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        ttl_seconds: float = 300.0,
        *,
        claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds must be positive")
        self._session_factory = session_factory
        self._ttl = timedelta(seconds=ttl_seconds)
        self._claim_lease = timedelta(seconds=claim_lease_seconds)

    def save(self, event: InboundEvent, case_ref: str | None) -> PendingPassiveReply | None:
        """Persist one callback context, returning ``None`` without a req_id."""
        raw_frame = _raw_frame(event)
        req_id = _callback_req_id(event)
        if raw_frame is None or req_id is None:
            return None
        clean_case_ref = case_ref.strip().upper() if case_ref else None
        now = _utc_now()
        expires_at = now + self._ttl
        record = DeferredPassiveReply(
            id=uuid4(),
            case_ref=clean_case_ref,
            req_id=req_id,
            msgid=event.msgid,
            raw_frame_json=raw_frame,
            expires_at=expires_at,
        )
        with self._session_factory() as session, session.begin():
            self._purge_expired(session, now)
            session.add(record)
            session.flush()
        return PendingPassiveReply(
            id=record.id,
            case_ref=clean_case_ref,
            req_id=req_id,
            event=event,
            expires_at=expires_at,
            claim_token=None,
        )

    def take(self, case_ref: str) -> PendingPassiveReply | None:
        """Claim a case match, or the sole unkeyed callback if unambiguous."""
        now = _utc_now()
        clean_case_ref = case_ref.strip().upper()
        with self._session_factory() as session, session.begin():
            self._purge_expired(session, now)
            eligible = self._eligible(now)
            record = session.scalar(
                select(DeferredPassiveReply)
                .where(DeferredPassiveReply.case_ref == clean_case_ref, eligible)
                .order_by(DeferredPassiveReply.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if record is None:
                unkeyed = session.scalars(
                    select(DeferredPassiveReply)
                    .where(DeferredPassiveReply.case_ref.is_(None), eligible)
                    .order_by(DeferredPassiveReply.created_at.asc())
                    .limit(2)
                    .with_for_update(skip_locked=True)
                ).all()
                if len(unkeyed) == 1:
                    record = unkeyed[0]
            if record is None:
                return None
            claim_token = uuid4()
            record.claim_token = claim_token
            record.claimed_until = now + self._claim_lease
            return self._pending_from_record(record, claim_token=claim_token)

    def acknowledge(self, pending: PendingPassiveReply) -> None:
        """Delete a successfully handled callback context."""
        if pending.claim_token is None:
            return
        with self._session_factory() as session, session.begin():
            record = session.scalar(
                select(DeferredPassiveReply)
                .where(
                    DeferredPassiveReply.id == pending.id,
                    DeferredPassiveReply.claim_token == pending.claim_token,
                )
                .with_for_update()
            )
            if record is not None:
                session.delete(record)

    def release(self, pending: PendingPassiveReply) -> None:
        """Make a claimed context available again after an interrupted attempt."""
        if pending.claim_token is None:
            return
        with self._session_factory() as session, session.begin():
            record = session.scalar(
                select(DeferredPassiveReply)
                .where(
                    DeferredPassiveReply.id == pending.id,
                    DeferredPassiveReply.claim_token == pending.claim_token,
                )
                .with_for_update()
            )
            if record is not None:
                record.claim_token = None
                record.claimed_until = None

    def has_match(self, case_ref: str) -> bool:
        """Tell the worker whether a later consultant reply can release a callback."""
        now = _utc_now()
        clean_case_ref = case_ref.strip().upper()
        with self._session_factory() as session, session.begin():
            self._purge_expired(session, now)
            eligible = self._eligible(now)
            keyed = session.scalar(
                select(DeferredPassiveReply.id)
                .where(DeferredPassiveReply.case_ref == clean_case_ref, eligible)
                .limit(1)
            )
            if keyed is not None:
                return True
            unkeyed = session.scalars(
                select(DeferredPassiveReply.id)
                .where(DeferredPassiveReply.case_ref.is_(None), eligible)
                .limit(2)
            ).all()
            return len(unkeyed) == 1

    def purge_expired(self) -> int:
        """Remove callback contexts whose TTL has elapsed."""
        with self._session_factory() as session, session.begin():
            return self._purge_expired(session, _utc_now())

    def __len__(self) -> int:
        with self._session_factory() as session, session.begin():
            now = _utc_now()
            self._purge_expired(session, now)
            return len(session.scalars(select(DeferredPassiveReply.id)).all())

    def _pending_from_record(
        self, record: DeferredPassiveReply, *, claim_token: UUID | None
    ) -> PendingPassiveReply:
        raw_frame = dict(cast(Mapping[str, object], record.raw_frame_json))
        return PendingPassiveReply(
            id=record.id,
            case_ref=record.case_ref,
            req_id=record.req_id,
            event=InboundEvent(
                msgid=record.msgid,
                sender_userid="",
                chatid=None,
                chattype="group",
                parts=(),
                metadata={"wecom_raw_frame": raw_frame},
            ),
            expires_at=record.expires_at,
            claim_token=claim_token,
        )

    def _eligible(self, now: datetime):
        return or_(
            DeferredPassiveReply.claim_token.is_(None),
            DeferredPassiveReply.claimed_until.is_(None),
            DeferredPassiveReply.claimed_until <= now,
        )

    def _purge_expired(self, session: Session, now: datetime) -> int:
        result = session.execute(
            delete(DeferredPassiveReply).where(DeferredPassiveReply.expires_at <= now)
        )
        return int(result.rowcount or 0)


def _raw_frame(event: InboundEvent) -> dict[str, object] | None:
    raw_frame = event.metadata.get("wecom_raw_frame")
    if not isinstance(raw_frame, Mapping):
        return None
    return dict(cast(Mapping[str, object], raw_frame))


def _callback_req_id(event: InboundEvent) -> str | None:
    raw_frame = _raw_frame(event)
    if raw_frame is None:
        return None
    headers = raw_frame.get("headers")
    if not isinstance(headers, Mapping):
        return None
    req_id = headers.get("req_id")
    if not isinstance(req_id, str) or not req_id.strip():
        return None
    return req_id.strip()
