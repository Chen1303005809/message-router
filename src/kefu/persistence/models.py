"""Persistent records for the MVP.

The schema is intentionally organized around the event timeline and delivery
bundle, rather than around thin CRUD repositories.  Business transitions live
in :mod:`kefu.case_desk.service`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_CASE_DEADLINE_HOURS = 3
DEFAULT_APPROACHING_WINDOW_MINUTES = 24 * 60
MIN_DEADLINE_INCREMENT_MINUTES = 30
MAX_DEADLINE_INCREMENT_MINUTES = 2_147_483_640


def utc_now() -> datetime:
    return datetime.now(UTC)


def _default_case_deadline() -> datetime:
    return utc_now() + timedelta(hours=DEFAULT_CASE_DEADLINE_HOURS)


class Base(DeclarativeBase):
    pass


class TeamKind(StrEnum):
    CONSULT_QUEUE = "consult_queue"
    DEV = "dev"


class MembershipRole(StrEnum):
    MEMBER = "member"
    ADMIN = "admin"


class LifecycleStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class DeadlineStatus(StrEnum):
    APPROACHING = "approaching"
    OVERDUE = "overdue"


class WaitingOn(StrEnum):
    CONSULT = "consult"
    DEV = "dev"
    NONE = "none"


class CasePriority(StrEnum):
    NORMAL = "normal"
    URGENT = "urgent"
    SEVERE = "severe"


class CaseEntryKind(StrEnum):
    FORMAL_MESSAGE = "formal_message"
    TRANSFER_CONSULTANT = "transfer_consultant"
    TRANSFER_DEVELOPER = "transfer_developer"
    TRANSFER_DEV_TEAM = "transfer_dev_team"
    CLOSED = "closed"
    REOPENED = "reopened"
    CORRECTION = "correction"
    CASE_METADATA_UPDATED = "case_metadata_updated"


class EntrySide(StrEnum):
    CONSULT = "consult"
    DEV = "dev"
    SYSTEM = "system"


class MessageIntent(StrEnum):
    HANDOFF = "handoff"
    SYNC = "sync"


class PartKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"


class DeliveryDestination(StrEnum):
    USER = "user"
    CHAT = "chat"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class DeliveryItemStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


def enum_column(enum: type[StrEnum], *, length: int = 32) -> SqlEnum:
    """Store stable lowercase enum values on both PostgreSQL and SQLite."""
    return SqlEnum(
        enum,
        native_enum=False,
        length=length,
        values_callable=lambda items: [item.value for item in items],
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    wecom_userid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # A global administrator is intentionally a property of the stable
    # enterprise identity, not a membership in one particular business team.
    # This lets the H5 management center administer all queues and dev teams
    # without widening the meaning of a team-level ``admin`` membership.
    is_global_admin: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[TeamKind] = mapped_column(enum_column(TeamKind))
    name: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class TeamMembership(Base):
    __tablename__ = "team_memberships"

    team_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[MembershipRole] = mapped_column(
        enum_column(MembershipRole), default=MembershipRole.MEMBER
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WeComChannel(Base):
    __tablename__ = "wecom_channels"
    __table_args__ = (
        UniqueConstraint("chatid", name="uq_wecom_channels_chatid"),
        Index("ix_wecom_channels_active_team", "team_id", "active"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("teams.id"))
    chatid: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    initialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("case_ref", name="uq_cases_case_ref"),
        CheckConstraint("version >= 1", name="ck_cases_version_positive"),
        CheckConstraint("last_entry_sequence >= 0", name="ck_cases_sequence_nonnegative"),
        CheckConstraint(
            "(lifecycle_status = 'closed' AND waiting_on = 'none') OR "
            "(lifecycle_status = 'open' AND waiting_on IN ('consult', 'dev'))",
            name="ck_cases_lifecycle_waiting_consistent",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    case_ref: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(512))
    customer_name: Mapped[str] = mapped_column(
        String(256), default="未录入", server_default="未录入"
    )
    customer_contact_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    customer_contact_method: Mapped[str | None] = mapped_column(String(256), nullable=True)
    priority: Mapped[CasePriority] = mapped_column(
        enum_column(CasePriority, length=16),
        default=CasePriority.NORMAL,
        server_default=CasePriority.NORMAL.value,
    )
    deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_default_case_deadline
    )
    approaching_window_minutes: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_APPROACHING_WINDOW_MINUTES,
        server_default=str(DEFAULT_APPROACHING_WINDOW_MINUTES),
    )
    lifecycle_status: Mapped[LifecycleStatus] = mapped_column(
        enum_column(LifecycleStatus), default=LifecycleStatus.OPEN
    )
    waiting_on: Mapped[WaitingOn] = mapped_column(enum_column(WaitingOn), default=WaitingOn.CONSULT)
    consult_queue_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("teams.id"), index=True
    )
    current_consultant_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    current_dev_team_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("teams.id"), index=True
    )
    current_developer_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    creator_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    related_case_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cases.id"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    last_entry_sequence: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class CaseEntry(Base):
    __tablename__ = "case_entries"
    __table_args__ = (
        UniqueConstraint("case_id", "sequence", name="uq_case_entries_case_sequence"),
        UniqueConstraint("source_msgid", name="uq_case_entries_source_msgid"),
        Index("ix_case_entries_case_sequence", "case_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[CaseEntryKind] = mapped_column(enum_column(CaseEntryKind))
    side: Mapped[EntrySide] = mapped_column(enum_column(EntrySide))
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    actor_name_snapshot: Mapped[str] = mapped_column(String(256))
    message_intent: Mapped[MessageIntent | None] = mapped_column(
        enum_column(MessageIntent), nullable=True
    )
    source_msgid: Mapped[str | None] = mapped_column(String(256), nullable=True)
    corrects_entry_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("case_entries.id"), nullable=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class StoredMedia(Base):
    __tablename__ = "stored_media"
    __table_args__ = (UniqueConstraint("object_key", name="uq_stored_media_object_key"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    object_key: Mapped[str] = mapped_column(String(1024))
    mime_type: Mapped[str] = mapped_column(String(256))
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CaseEntryPart(Base):
    __tablename__ = "case_entry_parts"
    __table_args__ = (
        UniqueConstraint("entry_id", "position", name="uq_case_entry_parts_entry_position"),
        CheckConstraint("position >= 0", name="ck_case_entry_parts_nonnegative_position"),
        CheckConstraint(
            "(kind = 'text' AND text IS NOT NULL AND media_id IS NULL) OR "
            "(kind = 'image' AND media_id IS NOT NULL AND text IS NULL)",
            name="ck_case_entry_parts_payload_matches_kind",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    entry_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("case_entries.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[PartKind] = mapped_column(enum_column(PartKind))
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stored_media.id"), nullable=True
    )


class MessageDraft(Base):
    __tablename__ = "message_drafts"
    __table_args__ = (UniqueConstraint("source_msgid", name="uq_message_drafts_source_msgid"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), index=True
    )
    source_msgid: Mapped[str] = mapped_column(String(256))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MessageDraftPart(Base):
    __tablename__ = "message_draft_parts"
    __table_args__ = (
        UniqueConstraint("draft_id", "position", name="uq_message_draft_parts_draft_position"),
        CheckConstraint("position >= 0", name="ck_message_draft_parts_nonnegative_position"),
        CheckConstraint(
            "(kind = 'text' AND text IS NOT NULL AND media_id IS NULL) OR "
            "(kind = 'image' AND media_id IS NOT NULL AND text IS NULL)",
            name="ck_message_draft_parts_payload_matches_kind",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    draft_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("message_drafts.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[PartKind] = mapped_column(enum_column(PartKind))
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stored_media.id"), nullable=True
    )


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        Index("ix_deliveries_pending", "status", "next_attempt_at"),
        Index("ix_deliveries_entry", "entry_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    entry_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("case_entries.id", ondelete="CASCADE")
    )
    destination_type: Mapped[DeliveryDestination] = mapped_column(enum_column(DeliveryDestination))
    destination_address: Mapped[str] = mapped_column(String(256))
    status: Mapped[DeliveryStatus] = mapped_column(
        enum_column(DeliveryStatus), default=DeliveryStatus.PENDING
    )
    waiting_on_after_delivery: Mapped[WaitingOn | None] = mapped_column(
        enum_column(WaitingOn), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lock_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class DeliveryItem(Base):
    __tablename__ = "delivery_items"
    __table_args__ = (
        UniqueConstraint("delivery_id", "position", name="uq_delivery_items_delivery_position"),
        UniqueConstraint("req_id", name="uq_delivery_items_req_id"),
        CheckConstraint("position >= 0", name="ck_delivery_items_nonnegative_position"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("deliveries.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[PartKind] = mapped_column(enum_column(PartKind))
    req_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[DeliveryItemStatus] = mapped_column(
        enum_column(DeliveryItemStatus), default=DeliveryItemStatus.PENDING
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    platform_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class InboundMessage(Base):
    """Idempotency ledger for callbacks that do not create a formal entry yet."""

    __tablename__ = "inbound_messages"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    msgid: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DeferredPassiveReply(Base):
    """Short-lived callback context used by delayed passive replies."""

    __tablename__ = "deferred_passive_replies"
    __table_args__ = (
        Index("ix_deferred_passive_replies_case_expiry", "case_ref", "expires_at"),
        Index("ix_deferred_passive_replies_expiry", "expires_at"),
        Index("ix_deferred_passive_replies_claim_token", "claim_token"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    case_ref: Mapped[str | None] = mapped_column(String(32), nullable=True)
    req_id: Mapped[str] = mapped_column(String(256))
    msgid: Mapped[str] = mapped_column(String(256))
    raw_frame_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WebSession(Base):
    """Opaque, short-lived H5 session keyed by a hashed cookie value."""

    __tablename__ = "web_sessions"
    __table_args__ = (Index("ix_web_sessions_active", "user_id", "expires_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
