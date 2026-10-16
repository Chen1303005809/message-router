from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from kefu.messages.contracts import PushTarget
from kefu.observability import opaque_id_fingerprint
from kefu.persistence.models import (
    DeliveryDestination,
    DeliveryStatus,
    MessageOutbox,
    PartKind,
    utc_now,
)
from kefu.wecom.transport import OutboundMessage, WeComTransport

logger = logging.getLogger(__name__)
_RETRY_SECONDS = (5, 30, 120, 600)
_LOCK_SECONDS = 90


@dataclass(frozen=True, slots=True)
class EnqueuedMessage:
    id: UUID
    idempotent: bool


@dataclass(frozen=True, slots=True)
class _ClaimedMessage:
    id: UUID
    lock_token: UUID
    idempotency_key: str
    destination_type: DeliveryDestination
    destination_address: str
    content: str
    attempts: int


class MessagePush:
    """Queue typed group or person messages for reliable asynchronous push."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def enqueue(
        self,
        target: PushTarget,
        content: str,
        *,
        idempotency_key: str | None = None,
    ) -> EnqueuedMessage:
        if not content.strip():
            raise ValueError("推送消息不能为空")
        key = (idempotency_key or f"push:{uuid4()}").strip()
        if len(key) > 256:
            raise ValueError("消息幂等键不能超过256个字符")
        with self._session_factory() as session, session.begin():
            existing = session.scalar(
                select(MessageOutbox).where(MessageOutbox.idempotency_key == key)
            )
            if existing is not None:
                return EnqueuedMessage(existing.id, True)
            record = MessageOutbox(
                id=uuid4(),
                idempotency_key=key,
                destination_type=target.destination_type,
                destination_address=target.address.strip(),
                content=content,
                status=DeliveryStatus.PENDING,
                attempts=0,
                next_attempt_at=utc_now(),
            )
            try:
                with session.begin_nested():
                    session.add(record)
                    session.flush()
            except IntegrityError:
                # A concurrent tool call with the same key has already queued it.
                existing = session.scalar(
                    select(MessageOutbox).where(MessageOutbox.idempotency_key == key)
                )
                if existing is None:
                    raise
                return EnqueuedMessage(existing.id, True)
            return EnqueuedMessage(record.id, False)

    def _id_for_key(self, key: str) -> UUID | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(MessageOutbox.id).where(MessageOutbox.idempotency_key == key)
            )
            return row


class MessagePushWorker:
    """Send durable messages and retry transient failures with bounded backoff."""

    def __init__(self, session_factory, transport: WeComTransport) -> None:
        self._session_factory = session_factory
        self._transport = transport

    async def deliver_pending(self, limit: int = 20) -> int:
        claimed = self._claim(limit)
        sent = 0
        for message in claimed:
            try:
                await self._transport.send(
                    OutboundMessage(
                        destination_type=message.destination_type,
                        destination_address=message.destination_address,
                        req_id=f"outbox-{message.id}",
                        kind=PartKind.TEXT,
                        payload={"content": message.content},
                    )
                )
            except Exception as error:
                self._failed(message, error)
                logger.warning(
                    "message_push_failed id=%s destination_type=%s destination_fp=%s error=%s",
                    message.id,
                    message.destination_type,
                    opaque_id_fingerprint(message.destination_address),
                    str(error).replace("\n", " ")[:240],
                )
            else:
                self._succeeded(message)
                sent += 1
        return sent

    def _claim(self, limit: int) -> tuple[_ClaimedMessage, ...]:
        now = utc_now()
        claimed: list[_ClaimedMessage] = []
        with self._session_factory() as session, session.begin():
            records = session.scalars(
                select(MessageOutbox)
                .where(
                    MessageOutbox.status == DeliveryStatus.PENDING,
                    MessageOutbox.next_attempt_at <= now,
                    or_(MessageOutbox.locked_until.is_(None), MessageOutbox.locked_until <= now),
                )
                .order_by(MessageOutbox.created_at, MessageOutbox.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for record in records:
                token = uuid4()
                record.lock_token = token
                record.locked_until = now + timedelta(seconds=_LOCK_SECONDS)
                record.attempts += 1
                claimed.append(
                    _ClaimedMessage(
                        id=record.id,
                        lock_token=token,
                        idempotency_key=record.idempotency_key,
                        destination_type=record.destination_type,
                        destination_address=record.destination_address,
                        content=record.content,
                        attempts=record.attempts,
                    )
                )
        return tuple(claimed)

    def _succeeded(self, message: _ClaimedMessage) -> None:
        with self._session_factory() as session, session.begin():
            record = session.get(MessageOutbox, message.id)
            if record is None or record.lock_token != message.lock_token:
                return
            record.status = DeliveryStatus.SENT
            record.sent_at = datetime.now(UTC)
            record.lock_token = None
            record.locked_until = None
            record.last_error = None

    def _failed(self, message: _ClaimedMessage, error: Exception) -> None:
        with self._session_factory() as session, session.begin():
            record = session.get(MessageOutbox, message.id)
            if record is None or record.lock_token != message.lock_token:
                return
            record.last_error = str(error).replace("\n", " ")[:1000]
            record.lock_token = None
            record.locked_until = None
            if message.attempts > len(_RETRY_SECONDS):
                record.status = DeliveryStatus.FAILED
                return
            record.next_attempt_at = utc_now() + timedelta(
                seconds=_RETRY_SECONDS[message.attempts - 1]
            )
