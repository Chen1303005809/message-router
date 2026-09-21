"""Bot-worker process: WeCom long connection, relay and durable delivery loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from kefu.case_desk.errors import CaseDeskError
from kefu.case_desk.markers import format_case_marker
from kefu.case_desk.service import CaseDesk
from kefu.config import Settings
from kefu.media.storage import (
    MediaIngestor,
    ObjectStorage,
    object_storage_from_settings,
)
from kefu.persistence.database import create_engine_from_url, create_session_factory
from kefu.relay.delivery import DeliveryWorker
from kefu.relay.handler import Relay, RelayDecision, RelayDisposition
from kefu.relay.references import parse_quoted_case_ref
from kefu.wecom.deferred_reply import DeferredPassiveReplyStore, PendingPassiveReply
from kefu.wecom.transport import (
    FakeWeComAdapter,
    InboundEvent,
    InboundImagePart,
    InboundReply,
    InboundTextPart,
    LongConnectionWeComAdapter,
    ReplyAction,
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
            require_group_mention=settings.wecom_require_group_mention,
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


async def _inbound_loop(
    *,
    relay: Relay,
    transport: WeComTransport,
    web_base_url: str,
    deferred_replies: DeferredPassiveReplyStore | None = None,
) -> None:
    async for event in transport.events():
        try:
            suppress_group_delivery = await asyncio.to_thread(
                _should_suppress_group_delivery,
                event=event,
                deferred_replies=deferred_replies,
            )
            # Database and object-store adapters are synchronous; do not stall
            # heartbeats while they write a draft or a formal entry.
            decision = await asyncio.to_thread(
                relay.handle,
                event,
                suppress_group_delivery=suppress_group_delivery,
            )
        except Exception:
            logger.exception("relay failed for WeCom msgid=%s", event.msgid)
            continue
        logger.info(
            "processed WeCom msgid=%s disposition=%s case_ref=%s",
            event.msgid,
            decision.disposition,
            decision.case_ref,
        )
        reply = _reply_for_decision(decision, web_base_url)
        pending = await asyncio.to_thread(
            _defer_group_callback,
            event=event,
            decision=decision,
            deferred_replies=deferred_replies,
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
            except Exception:
                # The formal event/draft is already durable. A reply failure must
                # not cause the platform callback to be processed a second time.
                logger.exception("failed to reply to WeCom msgid=%s", event.msgid)
        await _release_deferred_callback(
            event=event,
            decision=decision,
            deferred_replies=deferred_replies,
            transport=transport,
            relay=relay,
            suppress_group_delivery=suppress_group_delivery,
        )


def _defer_group_callback(
    *,
    event: InboundEvent,
    decision: RelayDecision,
    deferred_replies: DeferredPassiveReplyStore | None,
) -> PendingPassiveReply | None:
    if (
        deferred_replies is None
        or decision.idempotent
        or event.chattype != "group"
        or not event.mentioned_bot
    ):
        return None
    if decision.disposition is RelayDisposition.FORWARDED:
        return deferred_replies.save(event, decision.case_ref)
    if (
        decision.disposition is RelayDisposition.REJECTED
        and decision.reply_text == DEFERRED_QUOTE_ERROR
    ):
        # The no-quote branch has no case_ref.  The store will only release it
        # when it is the sole unkeyed callback, so this experiment never guesses
        # between multiple unrelated group messages.
        return deferred_replies.save(event, None)
    return None


def _should_suppress_group_delivery(
    *,
    event: InboundEvent,
    deferred_replies: DeferredPassiveReplyStore | None,
) -> bool:
    """Suppress only a consultant group delivery that has a saved callback target."""
    if (
        deferred_replies is None
        or event.chattype != "single"
        or not event.quote_content
        or any(isinstance(part, InboundImagePart) for part in event.parts)
    ):
        return False
    try:
        case_ref = parse_quoted_case_ref(event.quote_content)
    except CaseDeskError:
        return False
    return deferred_replies.has_match(case_ref)


async def _release_deferred_callback(
    *,
    event: InboundEvent,
    decision: RelayDecision,
    deferred_replies: DeferredPassiveReplyStore | None,
    transport: WeComTransport,
    relay: Relay,
    suppress_group_delivery: bool,
) -> None:
    if (
        deferred_replies is None
        or decision.idempotent
        or decision.disposition is not RelayDisposition.FORWARDED
        or decision.case_ref is None
        or event.chattype != "single"
    ):
        return
    pending = await asyncio.to_thread(deferred_replies.take, decision.case_ref)
    if pending is None:
        if suppress_group_delivery and decision.delivery_ids:
            await _restore_deferred_deliveries(relay, decision.delivery_ids)
        return
    try:
        await transport.reply(
            pending.event,
            InboundReply(
                text=f"{_consultant_reply_text(event)}\n\n{format_case_marker(decision.case_ref)}"
            ),
        )
    except Exception:
        if suppress_group_delivery and decision.delivery_ids:
            await _restore_deferred_deliveries(relay, decision.delivery_ids)
        await _acknowledge_deferred_reply(deferred_replies, pending)
        logger.exception(
            "failed to release deferred passive reply req_id=%s case_ref=%s",
            pending.req_id,
            pending.case_ref,
        )
    else:
        if suppress_group_delivery and decision.delivery_ids:
            try:
                await asyncio.to_thread(relay.complete_deferred_deliveries, decision.delivery_ids)
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


async def _restore_deferred_deliveries(
    relay: Relay, delivery_ids: tuple[UUID, ...]
) -> None:
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


def _consultant_reply_text(event: InboundEvent) -> str:
    text = "\n".join(
        part.text for part in event.parts if isinstance(part, InboundTextPart) and part.text.strip()
    ).strip()
    return text or "咨询侧已回复。"


def _reply_for_decision(decision: RelayDecision, web_base_url: str) -> InboundReply | None:
    """Turn relay outcomes into low-noise, callback-bound bot responses."""
    if decision.idempotent:
        return None
    if decision.disposition is RelayDisposition.DRAFT_SAVED and decision.draft_id is not None:
        return InboundReply(
            title="消息草稿已保存",
            text=decision.reply_text or "请选择下一步操作。",
            actions=(
                ReplyAction("创建新事件", f"{web_base_url}/drafts/{decision.draft_id}/new"),
                ReplyAction("事件中心", f"{web_base_url}/events"),
            ),
        )
    if decision.disposition is RelayDisposition.OPEN_EVENT_CENTER:
        return InboundReply(
            title="客户问题事件中心",
            text="打开事件中心查看你有权限访问的事件。",
            actions=(ReplyAction("打开事件中心", f"{web_base_url}/events"),),
        )
    if decision.disposition is RelayDisposition.CHANNEL_BOUND and decision.reply_text:
        return InboundReply(title="研发群已绑定", text=decision.reply_text)
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
    transport = _transport_from_settings(settings, session_factory, storage)
    worker = DeliveryWorker(desk, transport)
    deferred_store = DeferredPassiveReplyStore(
        session_factory,
        settings.wecom_deferred_passive_reply_ttl_seconds,
    )
    deferred_store.purge_expired()
    deferred_replies = (
        deferred_store
        if settings.wecom_deferred_passive_reply_enabled
        else None
    )
    await transport.start()
    logger.info("bot-worker started with transport=%s", settings.wecom_transport)
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_delivery_loop(worker))
            tasks.create_task(
                _inbound_loop(
                    relay=relay,
                    transport=transport,
                    web_base_url=settings.web_base_url,
                    deferred_replies=deferred_replies,
                )
            )
    finally:
        await transport.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
