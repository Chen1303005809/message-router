"""Translate normalized WeCom group callbacks into CaseDesk commands."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import (
    Actor,
    ContentPart,
    GroupActor,
    ImagePart,
    PostFormalMessage,
    TextPart,
)
from kefu.case_desk.errors import CaseDeskError, ValidationError
from kefu.case_desk.service import CaseDesk
from kefu.media.storage import MediaIngestError, MediaIngestor
from kefu.observability import opaque_id_fingerprint
from kefu.persistence.models import EntrySide, InboundMessage, TeamKind
from kefu.relay.references import parse_quoted_case_ref
from kefu.routing.directory import DatabaseRoutingDirectory
from kefu.wecom.transport import InboundEvent, InboundImagePart, InboundPart, InboundTextPart

logger = logging.getLogger(__name__)


class RelayDisposition(StrEnum):
    FORWARDED = "forwarded"
    CHANNEL_BOUND = "channel_bound"
    CONSULT_CHANNEL_BOUND = "consult_channel_bound"
    IGNORED = "ignored"
    REJECTED = "rejected"
    PROCESSING = "processing"


@dataclass(frozen=True, slots=True)
class RelayDecision:
    disposition: RelayDisposition
    case_ref: str | None = None
    reply_text: str | None = None
    idempotent: bool = False
    delivery_ids: tuple[UUID, ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "case_ref": self.case_ref,
            "reply_text": self.reply_text,
            "idempotent": self.idempotent,
            "delivery_ids": [str(delivery_id) for delivery_id in self.delivery_ids],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> RelayDecision:
        try:
            disposition = RelayDisposition(str(data["disposition"]))
        except (KeyError, ValueError):
            # Old single-chat outcomes are no longer actionable, but their
            # idempotency rows can remain after deployment.
            disposition = RelayDisposition.IGNORED
        raw_delivery_ids = data.get("delivery_ids")
        delivery_ids = (
            tuple(UUID(str(delivery_id)) for delivery_id in raw_delivery_ids)
            if isinstance(raw_delivery_ids, (list, tuple))
            else ()
        )
        return cls(
            disposition=disposition,
            case_ref=cast(str | None, data.get("case_ref")),
            reply_text=cast(str | None, data.get("reply_text")),
            idempotent=True,
            delivery_ids=delivery_ids,
        )


class Relay:
    """Handle group binding commands and mentions in bound group chats."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        case_desk: CaseDesk,
        media_ingestor: MediaIngestor | None = None,
        directory: DatabaseRoutingDirectory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._case_desk = case_desk
        self._media_ingestor = media_ingestor
        self._directory = directory or DatabaseRoutingDirectory()

    def handle(
        self,
        event: InboundEvent,
        *,
        suppress_delivery: bool = False,
        origin_side: EntrySide | None = None,
        origin_side_resolved: bool = False,
    ) -> RelayDecision:
        """Classify a callback exactly once and return a transport-neutral decision."""
        if event.chattype not in {"group", "single"}:
            return RelayDecision(RelayDisposition.IGNORED)
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
                decision = self._handle_private_followup(event)
            else:
                decision = self._handle_group(
                    event,
                    suppress_delivery=suppress_delivery,
                    origin_side=origin_side,
                    origin_side_resolved=origin_side_resolved,
                )
        except (CaseDeskError, MediaIngestError) as error:
            logger.warning(
                "[GROUP-TRACE] message_rejected msgid=%s chatid_fp=%s error=%s",
                msgid,
                opaque_id_fingerprint(event.chatid),
                error,
            )
            decision = RelayDecision(RelayDisposition.REJECTED, reply_text=str(error))
        return self._remember_decision(msgid, decision)

    def _handle_private_followup(self, event: InboundEvent) -> RelayDecision:
        if not event.quote_content or not event.quote_content.strip():
            return RelayDecision(
                RelayDisposition.REJECTED,
                reply_text=(
                    "私聊新问题请通过机器人卡片进入 H5；跟进已有问题时请引用"
                    "含唯一问题编号的机器人消息。"
                ),
            )
        if not event.sender_userid:
            return RelayDecision(
                RelayDisposition.REJECTED,
                reply_text="无法识别私聊发送人，请联系管理员按企微明文 userid 登记成员后重试。",
            )
        with self._session_factory() as session:
            user = self._directory.active_user_by_exact_wecom_userid(
                session, event.sender_userid
            )
            if user is None:
                return RelayDecision(
                    RelayDisposition.REJECTED,
                    reply_text="无法识别私聊发送人，请联系管理员按企微明文 userid 登记成员后重试。",
                )
            actor = Actor(user.id)
        case_ref = parse_quoted_case_ref(event.quote_content)
        parts = self._to_case_parts(event.parts)
        result = self._case_desk.execute(
            PostFormalMessage(
                case_ref=case_ref,
                expected_version=None,
                parts=parts,
                intent=event.intent,
                source_msgid=event.msgid,
            ),
            actor,
        )
        logger.info(
            "private_followup_recorded msgid=%s sender_fp=%s case_ref=%s idempotent=%s",
            event.msgid,
            opaque_id_fingerprint(event.sender_userid),
            result.case_ref,
            result.idempotent,
        )
        return RelayDecision(
            RelayDisposition.FORWARDED,
            case_ref=result.case_ref,
            idempotent=result.idempotent,
            delivery_ids=result.delivery_ids,
        )

    @staticmethod
    def is_channel_binding_attempt(event: InboundEvent) -> bool:
        """Identify binding commands before route lookup, including malformed ones."""
        commands = ("绑定研发团队", "绑定咨询队列")
        return event.chattype == "group" and any(
            isinstance(part, InboundTextPart)
            and any(command in part.text for command in commands)
            for part in event.parts
        )

    def group_side_for_chatid(self, chatid: str | None) -> EntrySide | None:
        if not chatid:
            return None
        with self._session_factory() as session:
            team_kind = self._directory.team_kind_for_chatid(session, chatid)
        if team_kind is TeamKind.CONSULT_QUEUE:
            return EntrySide.CONSULT
        if team_kind is TeamKind.DEV:
            return EntrySide.DEV
        return None

    def active_actor_for_exact_wecom_userid(self, wecom_userid: str | None) -> Actor | None:
        """Resolve an active registered human without guessing encrypted IDs."""
        with self._session_factory() as session:
            user = self._directory.active_user_by_exact_wecom_userid(session, wecom_userid)
            return Actor(user.id) if user is not None else None

    def _handle_group(
        self,
        event: InboundEvent,
        *,
        suppress_delivery: bool = False,
        origin_side: EntrySide | None = None,
        origin_side_resolved: bool = False,
    ) -> RelayDecision:
        if not event.mentioned_bot:
            return RelayDecision(RelayDisposition.IGNORED)
        if not event.chatid:
            raise ValidationError("群聊消息缺少群聊标识")
        requested_binding = _requested_team_binding(event.parts)
        if requested_binding is not None:
            team_kind, team_name = requested_binding
            with self._session_factory() as session, session.begin():
                self._directory.bind_team_chat(
                    session,
                    team_name=team_name,
                    chatid=event.chatid,
                    team_kind=team_kind,
                )
            logger.info(
                "[GROUP-TRACE] binding_committed msgid=%s chatid_fp=%s team_kind=%s team_name=%r",
                event.msgid,
                opaque_id_fingerprint(event.chatid),
                team_kind,
                team_name,
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

        side = (
            origin_side
            if origin_side_resolved
            else self.group_side_for_chatid(event.chatid)
        )
        if side is None:
            raise ValidationError("当前群聊尚未绑定为咨询队列或研发责任团队")
        logger.info(
            "[GROUP-TRACE] route_selected msgid=%s chatid_fp=%s origin_side=%s",
            event.msgid,
            opaque_id_fingerprint(event.chatid),
            side,
        )
        if not event.quote_content:
            raise ValidationError("请引用机器人发送的文字片段后再 @机器人 回复")
        case_ref = parse_quoted_case_ref(event.quote_content)
        parts = self._to_case_parts(event.parts)
        result = self._case_desk.execute(
            PostFormalMessage(
                case_ref=case_ref,
                expected_version=None,
                parts=parts,
                intent=event.intent,
                source_msgid=event.msgid,
                side=side,
                origin_chatid=event.chatid,
                suppress_delivery=suppress_delivery,
            ),
            GroupActor(side, self._group_author_id(event, side)),
        )
        logger.info(
            "[GROUP-TRACE] message_recorded msgid=%s chatid_fp=%s origin_side=%s "
            "case_ref=%s idempotent=%s delivery_count=%s",
            event.msgid,
            opaque_id_fingerprint(event.chatid),
            side,
            result.case_ref,
            result.idempotent,
            len(result.delivery_ids),
        )
        return RelayDecision(
            RelayDisposition.FORWARDED,
            case_ref=result.case_ref,
            idempotent=result.idempotent,
            delivery_ids=result.delivery_ids,
        )

    def _group_author_id(self, event: InboundEvent, side: EntrySide) -> UUID | None:
        if event.sender_userid is None or event.chatid is None:
            return None
        try:
            with self._session_factory() as session:
                team = self._directory.active_team_for_chatid(session, event.chatid)
                expected_kind = (
                    TeamKind.CONSULT_QUEUE if side is EntrySide.CONSULT else TeamKind.DEV
                )
                if team.kind is not expected_kind:
                    return None
                user = self._directory.active_member_by_exact_wecom_userid(
                    session,
                    wecom_userid=event.sender_userid,
                    team_id=team.id,
                )
                if user is None:
                    logger.info(
                        "group_sender_identity_resolved msgid=%s sender_fp=%s matched=false",
                        event.msgid,
                        opaque_id_fingerprint(event.sender_userid),
                    )
                    return None
                logger.info(
                    "group_sender_identity_resolved msgid=%s sender_fp=%s matched=true",
                    event.msgid,
                    opaque_id_fingerprint(event.sender_userid),
                )
                return user.id
        except CaseDeskError:
            return None

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
            # Another worker won the unique msgid race; duplicate case entries
            # are still prevented by the source_msgid constraint.
            previous = self._previous_decision(msgid)
            if previous is not None:
                return previous
            return RelayDecision(RelayDisposition.PROCESSING, idempotent=True)

    def complete_deferred_deliveries(self, delivery_ids: Sequence[UUID]) -> int:
        """Finalize deliveries represented by a successful passive reply."""
        return self._case_desk.complete_deferred_deliveries(delivery_ids)

    def restore_deferred_deliveries(self, delivery_ids: Sequence[UUID]) -> int:
        """Restore deliveries when the passive callback cannot be sent."""
        return self._case_desk.restore_deferred_deliveries(delivery_ids)

    def get_delivery_markdown_content(self, delivery_id: UUID) -> str | None:
        """Return the persisted Markdown body for a formal message delivery."""
        return self._case_desk.get_delivery_markdown_content(delivery_id)


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
