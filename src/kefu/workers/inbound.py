"""WeCom inbound event handling and passive reply coordination."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from urllib.parse import urlencode
from uuid import UUID

from kefu.case_desk.contracts import ImagePart, TextPart
from kefu.case_desk.errors import CaseDeskError
from kefu.case_desk.markers import parse_case_refs
from kefu.config import Settings
from kefu.issues.group_intake import GroupIssueIntakeService
from kefu.issues.service import IssueManagement
from kefu.media.storage import MediaIngestor
from kefu.messages.receiver import MessageReceiver
from kefu.observability import opaque_id_fingerprint
from kefu.persistence.models import EntrySide
from kefu.relay.handler import Relay, RelayDecision, RelayDisposition
from kefu.relay.references import parse_quoted_case_ref
from kefu.wecom.deferred_reply import DeferredPassiveReplyStore, PendingPassiveReply
from kefu.wecom.transport import (
    InboundEvent,
    InboundImagePart,
    InboundReply,
    InboundTextPart,
    WeComTransport,
)

logger = logging.getLogger(__name__)
DEFERRED_QUOTE_ERROR = "请引用机器人发送的文字片段后再 @机器人 回复"


async def _inbound_loop(
    *,
    relay: Relay,
    transport: WeComTransport,
    receiver: MessageReceiver | None = None,
    deferred_replies: DeferredPassiveReplyStore | None = None,
    group_intake: GroupIssueIntakeService | None = None,
    private_intake: IssueManagement | None = None,
    media_ingestor: MediaIngestor | None = None,
    settings: Settings | None = None,
) -> None:
    async for event in transport.events():
        if event.chattype == "single":
            await _handle_private_inbound(
                event,
                relay=relay,
                transport=transport,
                deferred_replies=deferred_replies,
                private_intake=private_intake,
                media_ingestor=media_ingestor,
                settings=settings,
            )
            continue
        if event.chattype == "group":
            raw_quote = _raw_quote(event)
            raw_quote_refs = sorted(_case_refs_in_value(raw_quote))
            normalized_quote_refs = sorted(parse_case_refs(event.quote_content or ""))
            raw_body = _raw_body(event)
            raw_message_type = _log_label(raw_body.get("msgtype")) if raw_body else "unknown"
            raw_quote_type = _quote_type_for_log(raw_quote)
            logger.info(
                "[INBOUND-DIAG] callback_normalized msgid=%s message_type=%s "
                "quote_type=%s quote_present=%s quote_chars=%s normalized_quote_refs=%s "
                "raw_quote_refs=%s part_types=%s decode_error=%s",
                event.msgid,
                raw_message_type,
                raw_quote_type,
                bool(event.quote_content),
                len(event.quote_content or ""),
                ",".join(normalized_quote_refs) or "none",
                ",".join(raw_quote_refs) or "none",
                ",".join(type(part).__name__ for part in event.parts) or "none",
                event.metadata.get("decode_error", "none"),
            )
            logger.info(
                "[GROUP-TRACE] callback_received msgid=%s chatid_fp=%s chatid_len=%s "
                "mentioned=%s quote_present=%s part_count=%s",
                event.msgid,
                opaque_id_fingerprint(event.chatid),
                len((event.chatid or "").strip()),
                event.mentioned_bot,
                bool(event.quote_content),
                len(event.parts),
            )
        try:
            origin_side_resolved = (
                event.chattype == "group"
                and event.mentioned_bot
                and not relay.is_channel_binding_attempt(event)
            )
            origin_side = (
                await asyncio.to_thread(relay.group_side_for_chatid, event.chatid)
                if origin_side_resolved
                else None
            )
            if (
                group_intake is not None
                and not relay.is_channel_binding_attempt(event)
                and group_intake.should_handle(event, origin_side)
            ):
                try:
                    intake_reply = await group_intake.handle(event)
                    await transport.reply(event, InboundReply(text=intake_reply))
                    logger.info(
                        "group_issue_intake_callback_replied msgid=%s chatid_fp=%s",
                        event.msgid,
                        opaque_id_fingerprint(event.chatid),
                    )
                except Exception as error:
                    logger.error(
                        "group_issue_intake_callback_failed msgid=%s chatid_fp=%s error_type=%s",
                        event.msgid,
                        opaque_id_fingerprint(event.chatid),
                        type(error).__name__,
                    )
                    try:
                        await transport.reply(
                            event,
                            InboundReply(text="问题受理暂时失败，请引用提案或重新 @机器人 重试。"),
                        )
                    except Exception as error:
                        logger.error(
                            "group_issue_intake_callback_error_reply_failed msgid=%s error_type=%s",
                            event.msgid,
                            type(error).__name__,
                        )
                continue
            suppress_delivery = await asyncio.to_thread(
                _deferred_delivery_suppression,
                event=event,
                deferred_replies=deferred_replies,
                origin_side=origin_side,
            )
            # Database and object-store adapters are synchronous; do not stall
            # heartbeats while they write a formal entry.
            decision = await asyncio.to_thread(
                receiver.receive_wecom if receiver is not None else relay.handle,
                event,
                suppress_delivery=suppress_delivery,
                origin_side=origin_side,
                origin_side_resolved=origin_side_resolved,
            )
        except Exception as error:
            logger.error(
                "[GROUP-TRACE] relay_failed msgid=%s chatid_fp=%s error_type=%s",
                event.msgid,
                opaque_id_fingerprint(event.chatid),
                type(error).__name__,
            )
            continue
        logger.info(
            "[GROUP-TRACE] callback_processed msgid=%s chatid_fp=%s disposition=%s "
            "case_ref=%s idempotent=%s",
            event.msgid,
            opaque_id_fingerprint(event.chatid),
            decision.disposition,
            decision.case_ref,
            decision.idempotent,
        )
        reply = _reply_for_decision(decision)
        pending = await asyncio.to_thread(
            _defer_group_callback,
            event=event,
            decision=decision,
            deferred_replies=deferred_replies,
            origin_side=origin_side,
        )
        if pending is not None:
            logger.info(
                "deferred passive WeCom reply msgid=%s case_ref=%s",
                event.msgid,
                pending.case_ref,
            )
        elif reply is not None:
            try:
                await transport.reply(event, reply)
                logger.info(
                    "[GROUP-TRACE] callback_reply_sent msgid=%s chatid_fp=%s",
                    event.msgid,
                    opaque_id_fingerprint(event.chatid),
                )
            except Exception as error:
                # The formal group event is already durable. A reply failure must
                # not cause the platform callback to be processed a second time.
                logger.error(
                    "[GROUP-TRACE] callback_reply_failed msgid=%s chatid_fp=%s error_type=%s",
                    event.msgid,
                    opaque_id_fingerprint(event.chatid),
                    type(error).__name__,
                )
        await _release_deferred_callback(
            event=event,
            decision=decision,
            deferred_replies=deferred_replies,
            transport=transport,
            relay=relay,
            origin_side=origin_side,
            suppress_delivery=suppress_delivery,
        )


async def _handle_private_inbound(
    event: InboundEvent,
    *,
    relay: Relay,
    transport: WeComTransport,
    deferred_replies: DeferredPassiveReplyStore | None,
    private_intake: IssueManagement | None,
    media_ingestor: MediaIngestor | None,
    settings: Settings | None,
) -> None:
    sender_fp = opaque_id_fingerprint(event.sender_userid)
    try:
        actor = await asyncio.to_thread(
            relay.active_actor_for_exact_wecom_userid,
            event.sender_userid,
        )
        logger.info(
            "private_sender_identity_resolved msgid=%s sender_fp=%s matched=%s",
            event.msgid,
            sender_fp,
            actor is not None,
        )
        if actor is None:
            await transport.reply(
                event,
                InboundReply(
                    text=(
                        "无法识别机器人私聊身份，请联系管理员按企微明文 userid完成成员登记后重试。"
                    )
                ),
            )
            return
        decode_error = event.metadata.get("decode_error")
        if isinstance(decode_error, str) and decode_error:
            await transport.reply(event, InboundReply(text=decode_error))
            return
        if event.quote_content and event.quote_content.strip():
            origin_side = await asyncio.to_thread(relay.private_followup_side, event)
            suppress_delivery = await asyncio.to_thread(
                _deferred_delivery_suppression,
                event=event,
                deferred_replies=deferred_replies,
                origin_side=origin_side,
            )
            logger.info(
                "private_followup_delivery_suppression msgid=%s side=%s suppressed=%s",
                event.msgid,
                origin_side,
                suppress_delivery,
            )
            decision = await asyncio.to_thread(
                relay.handle,
                event,
                suppress_delivery=suppress_delivery,
            )
            if decision.reply_text and not decision.idempotent:
                await transport.reply(event, InboundReply(text=decision.reply_text))
            await _release_deferred_callback(
                event=event,
                decision=decision,
                deferred_replies=deferred_replies,
                transport=transport,
                relay=relay,
                origin_side=decision.origin_side,
                suppress_delivery=suppress_delivery,
            )
            logger.info(
                "private_followup_processed msgid=%s sender_fp=%s disposition=%s "
                "case_ref=%s idempotent=%s",
                event.msgid,
                sender_fp,
                decision.disposition,
                decision.case_ref,
                decision.idempotent,
            )
            return

        text_parts = [part.text for part in event.parts if isinstance(part, InboundTextPart)]
        plain_text = "".join(text_parts).strip()
        has_images = any(isinstance(part, InboundImagePart) for part in event.parts)
        if not has_images and plain_text in {"我的问题", "我的事件"}:
            if settings is None:
                raise RuntimeError("私聊入口缺少 H5 配置")
            await transport.reply(
                event,
                InboundReply(
                    text="打开 H5 查看问题列表",
                    template_card=_private_link_card(
                        title="我的问题",
                        subtitle="进入问题中心查看你负责的问题。",
                        url=f"{settings.web_base_url}/issues?{urlencode({'state': 'mine'})}",
                    ),
                ),
            )
            return
        if private_intake is None or media_ingestor is None or settings is None:
            raise RuntimeError("私聊问题受理尚未配置")
        if not plain_text:
            await transport.reply(
                event,
                InboundReply(text="私聊新问题请附上文字描述；图片可以和文字一起发送。"),
            )
            return

        content_parts: list[TextPart | ImagePart] = []
        raw_text_parts: list[str] = []
        for part in event.parts:
            if isinstance(part, InboundTextPart):
                content_parts.append(TextPart(part.text))
                raw_text_parts.append(part.text)
            elif isinstance(part, InboundImagePart):
                media_id = await asyncio.to_thread(media_ingestor.ingest, part)
                content_parts.append(ImagePart(media_id))
                raw_text_parts.append("[图片]")
        raw_text = "".join(raw_text_parts).strip()
        token = await asyncio.to_thread(
            private_intake.create_private_intake_handoff,
            msgid=event.msgid,
            actor_id=actor.user_id,
            raw_text=raw_text,
            parts=content_parts,
        )
        handoff_url = f"{settings.web_base_url}/issues/private-intake#{token}"
        await transport.reply(
            event,
            InboundReply(
                text="已收到私聊问题，请打开 H5 选择队列并确认后建单。",
                template_card=_private_link_card(
                    title="继续提交私聊问题",
                    subtitle="已带入本次文字和图片；选择队列及研发目标，审核确认后才会创建问题。",
                    url=handoff_url,
                ),
            ),
        )
        logger.info(
            "private_intake_handoff_created msgid=%s sender_fp=%s image_count=%s",
            event.msgid,
            sender_fp,
            sum(isinstance(part, ImagePart) for part in content_parts),
        )
    except (CaseDeskError, ValueError) as error:
        try:
            await transport.reply(event, InboundReply(text=str(error)))
        except Exception as reply_error:
            logger.error(
                "private_callback_error_reply_failed msgid=%s error_type=%s",
                event.msgid,
                type(reply_error).__name__,
            )
    except Exception as error:
        logger.error(
            "private_callback_failed msgid=%s sender_fp=%s error_type=%s",
            event.msgid,
            sender_fp,
            type(error).__name__,
        )
        try:
            await transport.reply(
                event,
                InboundReply(text="私聊消息暂时无法处理，请稍后重试。"),
            )
        except Exception as reply_error:
            logger.error(
                "private_callback_failure_reply_failed msgid=%s error_type=%s",
                event.msgid,
                type(reply_error).__name__,
            )


def _private_link_card(*, title: str, subtitle: str, url: str) -> Mapping[str, object]:
    return {
        "card_type": "text_notice",
        "main_title": {"title": title},
        "sub_title_text": subtitle,
        "card_action": {"type": 1, "url": url},
    }


def _raw_body(event: InboundEvent) -> Mapping[str, object] | None:
    frame = event.metadata.get("wecom_raw_frame")
    if not isinstance(frame, Mapping):
        return None
    body = frame.get("body")
    return body if isinstance(body, Mapping) else None


def _raw_quote(event: InboundEvent) -> object | None:
    body = _raw_body(event)
    return body.get("quote") if body is not None else None


def _case_refs_in_value(value: object, depth: int = 0) -> set[str]:
    """Find only safe event-reference markers inside a raw quote payload."""
    if depth > 8:
        return set()
    if isinstance(value, str):
        return set(parse_case_refs(value))
    if isinstance(value, Mapping):
        refs: set[str] = set()
        for child in list(value.values())[:100]:
            refs.update(_case_refs_in_value(child, depth + 1))
        return refs
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        refs: set[str] = set()
        for child in value[:100]:
            refs.update(_case_refs_in_value(child, depth + 1))
        return refs
    return set()


def _log_label(value: object) -> str:
    if not isinstance(value, str):
        return type(value).__name__ if value is not None else "absent"
    return " ".join(value.split())[:64] or "empty"


def _quote_type_for_log(value: object) -> str:
    if value is None:
        return "absent"
    if isinstance(value, Mapping):
        return _log_label(value.get("msgtype"))
    return type(value).__name__


def _defer_group_callback(
    *,
    event: InboundEvent,
    decision: RelayDecision,
    deferred_replies: DeferredPassiveReplyStore | None,
    origin_side: EntrySide | None,
) -> PendingPassiveReply | None:
    if (
        deferred_replies is None
        or decision.idempotent
        or event.chattype != "group"
        or not event.mentioned_bot
        or origin_side is None
    ):
        return None
    if decision.disposition is RelayDisposition.FORWARDED:
        return deferred_replies.save(event, decision.case_ref, origin_side=origin_side)
    if (
        decision.disposition is RelayDisposition.REJECTED
        and decision.reply_text == DEFERRED_QUOTE_ERROR
    ):
        # The no-quote branch has no case_ref. The store releases it only when
        # exactly one unkeyed callback from this originating side is eligible.
        return deferred_replies.save(event, None, origin_side=origin_side)
    return None


def _deferred_delivery_suppression(
    *,
    event: InboundEvent,
    deferred_replies: DeferredPassiveReplyStore | None,
    origin_side: EntrySide | None,
) -> bool:
    """Return whether a matching passive group callback replaces the delivery."""
    if (
        deferred_replies is None
        or origin_side is None
        or not event.quote_content
        or any(isinstance(part, InboundImagePart) for part in event.parts)
    ):
        return False
    try:
        case_ref = parse_quoted_case_ref(event.quote_content)
    except CaseDeskError:
        return False
    if origin_side is EntrySide.CONSULT:
        return deferred_replies.has_match(case_ref, origin_side=EntrySide.DEV)
    return deferred_replies.has_match(case_ref, origin_side=EntrySide.CONSULT)


async def _release_deferred_callback(
    *,
    event: InboundEvent,
    decision: RelayDecision,
    deferred_replies: DeferredPassiveReplyStore | None,
    transport: WeComTransport,
    relay: Relay,
    origin_side: EntrySide | None,
    suppress_delivery: bool,
) -> None:
    if (
        deferred_replies is None
        or decision.idempotent
        or decision.disposition is not RelayDisposition.FORWARDED
        or decision.case_ref is None
        or origin_side is None
    ):
        return
    pending_origin_side = EntrySide.DEV if origin_side is EntrySide.CONSULT else EntrySide.CONSULT
    pending = await asyncio.to_thread(
        deferred_replies.take,
        decision.case_ref,
        origin_side=pending_origin_side,
    )
    deferred_delivery_ids = _deferred_delivery_ids(
        decision.delivery_ids,
        suppress_delivery=suppress_delivery,
    )
    if pending is None:
        if deferred_delivery_ids:
            await _restore_deferred_deliveries(relay, deferred_delivery_ids)
        return
    try:
        if not decision.delivery_ids:
            raise RuntimeError("正式消息缺少持久化投递，无法执行被动回复")
        markdown_content = await asyncio.to_thread(
            relay.get_delivery_markdown_content, decision.delivery_ids[0]
        )
        if not isinstance(markdown_content, str) or not markdown_content.strip():
            raise RuntimeError("正式消息缺少 Markdown 正文，无法执行被动回复")
        await transport.reply(
            pending.event,
            InboundReply(text=markdown_content),
        )
    except Exception as error:
        if deferred_delivery_ids:
            await _restore_deferred_deliveries(relay, deferred_delivery_ids)
        await _acknowledge_deferred_reply(deferred_replies, pending)
        logger.error(
            "failed to release deferred passive reply case_ref=%s error_type=%s",
            pending.case_ref,
            type(error).__name__,
        )
    else:
        if deferred_delivery_ids:
            try:
                await asyncio.to_thread(relay.complete_deferred_deliveries, deferred_delivery_ids)
            except Exception as error:
                # The passive reply has already reached WeCom. Keep the
                # placeholder marked as sent rather than generating a second
                # active message if bookkeeping is temporarily unavailable.
                logger.error(
                    "failed to finalize deferred group delivery case_ref=%s error_type=%s",
                    decision.case_ref,
                    type(error).__name__,
                )
        await _acknowledge_deferred_reply(deferred_replies, pending)
        logger.info(
            "released deferred passive WeCom reply case_ref=%s",
            pending.case_ref,
        )


def _deferred_delivery_ids(
    delivery_ids: tuple[UUID, ...],
    *,
    suppress_delivery: bool,
) -> tuple[UUID, ...]:
    return delivery_ids if suppress_delivery else ()


async def _restore_deferred_deliveries(relay: Relay, delivery_ids: tuple[UUID, ...]) -> None:
    try:
        await asyncio.to_thread(relay.restore_deferred_deliveries, delivery_ids)
    except Exception as error:
        logger.error(
            "failed to restore deferred group delivery error_type=%s",
            type(error).__name__,
        )


async def _acknowledge_deferred_reply(
    deferred_replies: DeferredPassiveReplyStore, pending: PendingPassiveReply
) -> None:
    try:
        await asyncio.to_thread(deferred_replies.acknowledge, pending)
    except Exception as error:
        logger.error(
            "failed to acknowledge deferred passive reply error_type=%s",
            type(error).__name__,
        )


def _reply_for_decision(decision: RelayDecision) -> InboundReply | None:
    """Turn relay outcomes into low-noise, callback-bound bot responses."""
    if decision.idempotent:
        return None
    if decision.disposition is RelayDisposition.CHANNEL_BOUND and decision.reply_text:
        return InboundReply(text=decision.reply_text)
    if decision.disposition is RelayDisposition.CONSULT_CHANNEL_BOUND and decision.reply_text:
        return InboundReply(text=decision.reply_text)
    if decision.disposition is RelayDisposition.REJECTED and decision.reply_text:
        return InboundReply(text=decision.reply_text)
    return None
