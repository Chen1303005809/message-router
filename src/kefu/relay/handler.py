"""Translate normalized WeCom callbacks into explicit CaseDesk commands."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import Actor, ContentPart, ImagePart, PostFormalMessage, TextPart
from kefu.case_desk.errors import CaseDeskError, ValidationError
from kefu.case_desk.service import CaseDesk, utc_now
from kefu.media.storage import MediaIngestError, MediaIngestor
from kefu.persistence.models import (
    EntrySide,
    InboundMessage,
    MessageDraft,
    MessageDraftPart,
    PartKind,
    TeamKind,
)
from kefu.relay.references import parse_quoted_case_ref
from kefu.routing.directory import DatabaseRoutingDirectory
from kefu.wecom.transport import InboundEvent, InboundImagePart, InboundPart, InboundTextPart

DEFAULT_DRAFT_TTL = timedelta(minutes=15)


class RelayDisposition(StrEnum):
    DRAFT_SAVED = "draft_saved"
    FORWARDED = "forwarded"
    CHANNEL_BOUND = "channel_bound"
    CONSULT_CHANNEL_BOUND = "consult_channel_bound"
    OPEN_EVENT_CENTER = "open_event_center"
    IGNORED = "ignored"
    REJECTED = "rejected"
    PROCESSING = "processing"


@dataclass(frozen=True, slots=True)
class RelayDecision:
    disposition: RelayDisposition
    case_ref: str | None = None
    draft_id: UUID | None = None
    reply_text: str | None = None
    idempotent: bool = False
    delivery_ids: tuple[UUID, ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "case_ref": self.case_ref,
            "draft_id": str(self.draft_id) if self.draft_id else None,
            "reply_text": self.reply_text,
            "idempotent": self.idempotent,
            "delivery_ids": [str(delivery_id) for delivery_id in self.delivery_ids],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> RelayDecision:
        draft_id = data.get("draft_id")
        raw_delivery_ids = data.get("delivery_ids")
        delivery_ids = (
            tuple(UUID(str(delivery_id)) for delivery_id in raw_delivery_ids)
            if isinstance(raw_delivery_ids, (list, tuple))
            else ()
        )
        return cls(
            disposition=RelayDisposition(str(data["disposition"])),
            case_ref=cast(str | None, data.get("case_ref")),
            draft_id=UUID(str(draft_id)) if draft_id else None,
            reply_text=cast(str | None, data.get("reply_text")),
            idempotent=True,
            delivery_ids=delivery_ids,
        )


class Relay:
    """Enforce explicit message affiliation before calling the event service."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        case_desk: CaseDesk,
        media_ingestor: MediaIngestor | None = None,
        directory: DatabaseRoutingDirectory | None = None,
        draft_ttl: timedelta = DEFAULT_DRAFT_TTL,
    ) -> None:
        self._session_factory = session_factory
        self._case_desk = case_desk
        self._media_ingestor = media_ingestor
        self._directory = directory or DatabaseRoutingDirectory()
        self._draft_ttl = draft_ttl

    def handle(
        self, event: InboundEvent, *, suppress_group_delivery: bool = False
    ) -> RelayDecision:
        """Classify a callback exactly once and return a transport-neutral decision."""
        msgid = event.msgid.strip()
        if not msgid or len(msgid) > 256:
            return RelayDecision(
                RelayDisposition.REJECTED,
                reply_text="消息标识无效，无法安全处理。",
            )
        previous = self._previous_decision(msgid)
        if previous is not None:
            return previous
        try:
            decode_error = event.metadata.get("decode_error")
            if isinstance(decode_error, str) and decode_error:
                decision = RelayDecision(RelayDisposition.REJECTED, reply_text=decode_error)
            elif event.chattype == "single":
                decision = self._handle_single(
                    event, suppress_group_delivery=suppress_group_delivery
                )
            elif event.chattype == "group":
                decision = self._handle_group(event)
            else:
                decision = RelayDecision(RelayDisposition.IGNORED)
        except (CaseDeskError, MediaIngestError) as error:
            decision = RelayDecision(RelayDisposition.REJECTED, reply_text=str(error))
        return self._remember_decision(msgid, decision)

    def _handle_single(
        self, event: InboundEvent, *, suppress_group_delivery: bool = False
    ) -> RelayDecision:
        sender = self._user_for_wecom_userid(event.sender_userid)
        if self._is_my_cases_shortcut(event.parts, event.quote_content):
            return RelayDecision(RelayDisposition.OPEN_EVENT_CENTER)
        if event.quote_content:
            case_ref = parse_quoted_case_ref(event.quote_content)
            parts = self._to_case_parts(event.parts)
            result = self._case_desk.execute(
                PostFormalMessage(
                    case_ref=case_ref,
                    expected_version=None,
                    parts=parts,
                    intent=event.intent,
                    source_msgid=event.msgid,
                    side=EntrySide.CONSULT,
                    suppress_delivery=suppress_group_delivery,
                ),
                Actor(sender),
            )
            return RelayDecision(
                RelayDisposition.FORWARDED,
                case_ref=result.case_ref,
                idempotent=result.idempotent,
                delivery_ids=result.delivery_ids,
            )
        parts = self._to_case_parts(event.parts)
        draft_id = self._save_draft(owner_user_id=sender, source_msgid=event.msgid, parts=parts)
        return RelayDecision(
            RelayDisposition.DRAFT_SAVED,
            draft_id=draft_id,
            reply_text="已保存为消息草稿。请选择“创建新事件”或添加到已有事件。",
        )

    def _handle_group(self, event: InboundEvent) -> RelayDecision:
        if not event.mentioned_bot:
            return RelayDecision(RelayDisposition.IGNORED)
        if not event.chatid:
            raise ValidationError("群聊消息缺少群聊标识")
        requested_binding = _requested_team_binding(event.parts)
        if requested_binding is not None:
            team_kind, team_name = requested_binding
            sender = self._user_for_wecom_userid(event.sender_userid)
            with self._session_factory() as session, session.begin():
                self._directory.bind_team_chat(
                    session,
                    actor_id=sender,
                    team_name=team_name,
                    chatid=event.chatid,
                    team_kind=team_kind,
                )
            if team_kind is TeamKind.CONSULT_QUEUE:
                return RelayDecision(
                    RelayDisposition.CONSULT_CHANNEL_BOUND,
                    reply_text=f"已将当前群绑定为咨询队列「{team_name}」的咨询群。",
                )
            return RelayDecision(
                RelayDisposition.CHANNEL_BOUND,
                reply_text=f"已将当前群绑定为研发团队「{team_name}」的沟通群。",
            )
        if not event.quote_content:
            raise ValidationError("请引用机器人发送的文字片段后再 @机器人 回复")
        case_ref = parse_quoted_case_ref(event.quote_content)
        sender = self._user_for_wecom_userid(event.sender_userid)
        # The service makes the matching check under the same transaction that
        # appends the entry; this preliminary read only avoids media work for a
        # caller who cannot even view the claimed event.
        self._case_desk.get_case(case_ref, Actor(sender))
        parts = self._to_case_parts(event.parts)
        result = self._case_desk.execute(
            PostFormalMessage(
                case_ref=case_ref,
                expected_version=None,
                parts=parts,
                intent=event.intent,
                source_msgid=event.msgid,
                side=EntrySide.DEV,
                origin_chatid=event.chatid,
            ),
            Actor(sender),
        )
        return RelayDecision(
            RelayDisposition.FORWARDED,
            case_ref=result.case_ref,
            idempotent=result.idempotent,
            delivery_ids=result.delivery_ids,
        )

    def _to_case_parts(self, inbound_parts: Sequence[InboundPart]) -> tuple[ContentPart, ...]:
        if not inbound_parts:
            raise ValidationError("消息不包含 MVP 支持的文字或图片内容")
        if not any(
            isinstance(part, InboundTextPart) and part.text.strip() for part in inbound_parts
        ):
            raise ValidationError("MVP 的正式消息必须至少包含一个文字片段")
        parts: list[ContentPart] = []
        for part in inbound_parts:
            if isinstance(part, InboundTextPart):
                parts.append(TextPart(part.text))
            elif isinstance(part, InboundImagePart):
                if self._media_ingestor is None:
                    raise MediaIngestError("图片转存服务尚未配置")
                parts.append(ImagePart(self._media_ingestor.ingest(part)))
            else:
                raise ValidationError("消息包含不支持的内容类型")
        return tuple(parts)

    def _save_draft(
        self, *, owner_user_id: UUID, source_msgid: str, parts: Sequence[ContentPart]
    ) -> UUID:
        with self._session_factory() as session, session.begin():
            existing = session.scalar(
                select(MessageDraft)
                .where(MessageDraft.source_msgid == source_msgid)
                .with_for_update()
            )
            if existing is not None:
                return existing.id
            draft = MessageDraft(
                id=uuid4(),
                owner_user_id=owner_user_id,
                source_msgid=source_msgid,
                expires_at=utc_now() + self._draft_ttl,
            )
            session.add(draft)
            for position, part in enumerate(parts):
                if isinstance(part, TextPart):
                    record = MessageDraftPart(
                        id=uuid4(),
                        draft_id=draft.id,
                        position=position,
                        kind=PartKind.TEXT,
                        text=part.text,
                        media_id=None,
                    )
                else:
                    record = MessageDraftPart(
                        id=uuid4(),
                        draft_id=draft.id,
                        position=position,
                        kind=PartKind.IMAGE,
                        text=None,
                        media_id=part.media_id,
                    )
                session.add(record)
            return draft.id

    def _user_for_wecom_userid(self, wecom_userid: str) -> UUID:
        with self._session_factory() as session:
            user = self._directory.get_user_by_wecom_userid(session, wecom_userid)
            return user.id

    def _previous_decision(self, msgid: str) -> RelayDecision | None:
        with self._session_factory() as session:
            record = session.scalar(select(InboundMessage).where(InboundMessage.msgid == msgid))
            if record is None:
                return None
            if record.result_json is None:
                return RelayDecision(
                    RelayDisposition.PROCESSING,
                    reply_text="消息正在处理，请稍后重试。",
                    idempotent=True,
                )
            return RelayDecision.from_json(record.result_json)

    def _remember_decision(self, msgid: str, decision: RelayDecision) -> RelayDecision:
        try:
            with self._session_factory() as session, session.begin():
                record = session.scalar(
                    select(InboundMessage).where(InboundMessage.msgid == msgid).with_for_update()
                )
                if record is not None:
                    if record.result_json is not None:
                        return RelayDecision.from_json(record.result_json)
                    record.result_json = decision.as_json()
                    return decision
                session.add(InboundMessage(id=uuid4(), msgid=msgid, result_json=decision.as_json()))
                return decision
        except IntegrityError:
            # Another worker won the unique msgid race.  A duplicate business
            # entry is still prevented by draft/entry source_msgid constraints.
            previous = self._previous_decision(msgid)
            if previous is not None:
                return previous
            return RelayDecision(RelayDisposition.PROCESSING, idempotent=True)

    def _is_my_cases_shortcut(
        self, parts: Sequence[InboundPart], quote_content: str | None
    ) -> bool:
        return (
            quote_content is None
            and len(parts) == 1
            and isinstance(parts[0], InboundTextPart)
            and parts[0].text.strip() == "我的事件"
        )

    def complete_deferred_deliveries(self, delivery_ids: Sequence[UUID]) -> int:
        """Finalize deliveries represented by a successful passive reply."""
        return self._case_desk.complete_deferred_deliveries(delivery_ids)

    def restore_deferred_deliveries(self, delivery_ids: Sequence[UUID]) -> int:
        """Restore deliveries when the passive callback cannot be sent."""
        return self._case_desk.restore_deferred_deliveries(delivery_ids)


def _requested_team_binding(parts: Sequence[InboundPart]) -> tuple[TeamKind, str] | None:
    """Parse explicit development-team or consultation-queue binding commands."""
    commands = (
        ("绑定研发团队", TeamKind.DEV, "研发团队"),
        ("绑定咨询队列", TeamKind.CONSULT_QUEUE, "咨询队列"),
    )
    for part in parts:
        if not isinstance(part, InboundTextPart):
            continue
        for command, team_kind, label in commands:
            if command not in part.text:
                continue
            team_name = part.text.split(command, maxsplit=1)[1].strip(" ：:\t\n")
            if not team_name:
                raise ValidationError(f"请使用“{command} 团队名称”指定要绑定的{label}")
            return team_kind, team_name
    return None
