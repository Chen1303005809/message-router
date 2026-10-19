"""Bot-worker process: WeCom long connection, relay and durable delivery loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from kefu.agent.service import Agent
from kefu.agent.status_worker import IssueStatusUpdateWorker
from kefu.case_desk.errors import CaseDeskError
from kefu.case_desk.markers import parse_case_refs
from kefu.case_desk.service import CaseDesk
from kefu.config import Settings
from kefu.inspection.service import IssueInspectionService
from kefu.media.storage import (
    MediaIngestor,
    ObjectStorage,
    object_storage_from_settings,
)
from kefu.messages.push import MessagePush, MessagePushWorker
from kefu.messages.receiver import MessageReceiver
from kefu.messages.recipients import RecipientResolver
from kefu.observability import database_target_fingerprint, opaque_id_fingerprint
from kefu.persistence.database import create_engine_from_url, create_session_factory
from kefu.persistence.models import EntrySide
from kefu.relay.delivery import DeliveryWorker
from kefu.relay.handler import Relay, RelayDecision, RelayDisposition
from kefu.relay.references import parse_quoted_case_ref
from kefu.wecom.deferred_reply import DeferredPassiveReplyStore, PendingPassiveReply
from kefu.wecom.transport import (
    FakeWeComAdapter,
    InboundEvent,
    InboundImagePart,
    InboundReply,
    LongConnectionWeComAdapter,
    TransportDisplacedError,
    WeComTransport,
)

logger = logging.getLogger(__name__)
DEFERRED_QUOTE_ERROR = "请引用机器人发送的文字片段后再 @机器人 回复"


def _storage_from_settings(settings: Settings) -> ObjectStorage:
    return object_storage_from_settings(settings)


def _transport_from_settings(
    settings: Settings,
    session_factory: Callable[[], Session],
    storage: ObjectStorage,
) -> WeComTransport:
    if settings.wecom_transport == "fake":
        return FakeWeComAdapter()
    if settings.wecom_transport == "long_connection":
        if not settings.wecom_bot_id or not settings.wecom_bot_secret:
            raise ValueError(
                "WECOM_TRANSPORT=long_connection 需要同时配置 WECOM_BOT_ID 和 WECOM_BOT_SECRET"
            )
        return LongConnectionWeComAdapter(
            bot_id=settings.wecom_bot_id,
            secret=settings.wecom_bot_secret,
            session_factory=session_factory,
            storage=storage,
            ws_url=settings.wecom_long_connection_url,
            bot_mention_name=settings.wecom_bot_mention_name,
            connect_timeout_seconds=settings.wecom_connect_timeout_seconds,
        )
    raise ValueError(f"不支持的企业微信传输模式：{settings.wecom_transport}")


async def _delivery_loop(worker: DeliveryWorker) -> None:
    while True:
        result = await worker.deliver_pending()
        if result.claimed_bundles:
            logger.info(
                "processed delivery bundles=%s sent_items=%s failed_bundles=%s",
                result.claimed_bundles,
                result.sent_items,
                result.failed_bundles,
            )
        await asyncio.sleep(1)


async def _message_delivery_loop(worker: MessagePushWorker) -> None:
    while True:
        sent = await worker.deliver_pending()
        if sent:
            logger.info("processed standalone messages sent=%s", sent)
        await asyncio.sleep(1)


async def _issue_status_update_loop(worker: IssueStatusUpdateWorker) -> None:
    while True:
        try:
            completed = await worker.process_pending()
            if completed:
                logger.info("processed issue status evaluations completed=%s", completed)
        except Exception:
            logger.exception("issue_status_update_worker_failed")
        await asyncio.sleep(1)


async def _inspection_loop(inspection: IssueInspectionService, settings: Settings) -> None:
    while True:
        try:
            result = await asyncio.to_thread(inspection.inspect)
            if (
                result.periodic_reminders
                or result.approaching_reminders
                or result.unresolved_targets
            ):
                logger.info(
                    "issue_inspection scanned=%s periodic=%s approaching=%s unresolved=%s",
                    result.scanned_issues,
                    result.periodic_reminders,
                    result.approaching_reminders,
                    result.unresolved_targets,
                )
        except Exception:
            logger.exception("issue_inspection_failed")
        await asyncio.sleep(settings.issue_inspection_scan_seconds)


async def _inbound_loop(
    *,
    relay: Relay,
    transport: WeComTransport,
    receiver: MessageReceiver | None = None,
    deferred_replies: DeferredPassiveReplyStore | None = None,
) -> None:
    async for event in transport.events():
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
        elif event.chattype == "single":
            logger.info("[GROUP-TRACE] private_message_ignored msgid=%s", event.msgid)
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
        except Exception:
            logger.exception(
                "[GROUP-TRACE] relay_failed msgid=%s chatid_fp=%s",
                event.msgid,
                opaque_id_fingerprint(event.chatid),
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
                "deferred passive WeCom reply msgid=%s req_id=%s case_ref=%s",
                event.msgid,
                pending.req_id,
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
            except Exception:
                # The formal group event is already durable. A reply failure must
                # not cause the platform callback to be processed a second time.
                logger.exception(
                    "[GROUP-TRACE] callback_reply_failed msgid=%s chatid_fp=%s",
                    event.msgid,
                    opaque_id_fingerprint(event.chatid),
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
    except Exception:
        if deferred_delivery_ids:
            await _restore_deferred_deliveries(relay, deferred_delivery_ids)
        await _acknowledge_deferred_reply(deferred_replies, pending)
        logger.exception(
            "failed to release deferred passive reply req_id=%s case_ref=%s",
            pending.req_id,
            pending.case_ref,
        )
    else:
        if deferred_delivery_ids:
            try:
                await asyncio.to_thread(relay.complete_deferred_deliveries, deferred_delivery_ids)
            except Exception:
                # The passive reply has already reached WeCom. Keep the
                # placeholder marked as sent rather than generating a second
                # active message if bookkeeping is temporarily unavailable.
                logger.exception(
                    "failed to finalize deferred group delivery case_ref=%s",
                    decision.case_ref,
                )
        await _acknowledge_deferred_reply(deferred_replies, pending)
        logger.info(
            "released deferred passive WeCom reply req_id=%s case_ref=%s",
            pending.req_id,
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
    except Exception:
        logger.exception("failed to restore deferred group delivery")


async def _acknowledge_deferred_reply(
    deferred_replies: DeferredPassiveReplyStore, pending: PendingPassiveReply
) -> None:
    try:
        await asyncio.to_thread(deferred_replies.acknowledge, pending)
    except Exception:
        logger.exception("failed to acknowledge deferred passive reply req_id=%s", pending.req_id)


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


async def run() -> None:
    settings = Settings.from_env()
    engine = create_engine_from_url(settings.database_url)
    session_factory = create_session_factory(engine)
    storage = _storage_from_settings(settings)
    desk = CaseDesk(session_factory, web_base_url=settings.web_base_url)
    recovered_deliveries = desk.recover_deferred_deliveries()
    if recovered_deliveries:
        logger.warning(
            "restored deferred group deliveries after worker restart: %s", recovered_deliveries
        )
    relay = Relay(
        session_factory,
        desk,
        media_ingestor=MediaIngestor(session_factory, storage),
    )
    message_push = MessagePush(session_factory)
    message_receiver = MessageReceiver(relay, message_push)
    recipients = RecipientResolver(session_factory)
    inspection = IssueInspectionService(
        session_factory,
        message_receiver,
        recipients,
        settings,
    )
    transport = _transport_from_settings(settings, session_factory, storage)
    worker = DeliveryWorker(desk, transport)
    message_worker = MessagePushWorker(session_factory, transport)
    deferred_store = DeferredPassiveReplyStore(
        session_factory,
        settings.wecom_deferred_passive_reply_ttl_seconds,
    )
    deferred_store.purge_expired()
    deferred_replies = deferred_store if settings.wecom_deferred_passive_reply_enabled else None
    status_agent = Agent(settings)
    status_update_worker = IssueStatusUpdateWorker(desk, status_agent)
    logger.info(
        "[GROUP-TRACE] bot_worker_starting transport=%s database_target_fp=%s",
        settings.wecom_transport,
        database_target_fingerprint(engine.url),
    )
    try:
        try:
            await transport.start()
        except TransportDisplacedError:
            logger.critical(
                "[GROUP-TRACE] bot_worker_stopped reason=connection_displaced "
                "action=remove_duplicate_worker_then_restart"
            )
            return
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(_delivery_loop(worker))
                tasks.create_task(_message_delivery_loop(message_worker))
                tasks.create_task(_issue_status_update_loop(status_update_worker))
                tasks.create_task(_inspection_loop(inspection, settings))
                tasks.create_task(
                    _inbound_loop(
                        relay=relay,
                        transport=transport,
                        receiver=message_receiver,
                        deferred_replies=deferred_replies,
                    )
                )
        except* TransportDisplacedError:
            logger.critical(
                "[GROUP-TRACE] bot_worker_stopped reason=connection_displaced "
                "action=remove_duplicate_worker_then_restart"
            )
    finally:
        try:
            await transport.close()
        finally:
            await status_agent.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
