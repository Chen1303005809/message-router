"""Bot-worker process: WeCom long connection, relay and durable delivery loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from kefu.agent.service import Agent
from kefu.agent.status_worker import IssueStatusUpdateWorker
from kefu.case_desk.service import CaseDesk
from kefu.config import Settings
from kefu.inspection.service import IssueInspectionService
from kefu.issues.group_intake import GroupIssueIntakeService
from kefu.issues.service import IssueManagement
from kefu.logging_setup import configure_logging
from kefu.media.storage import (
    MediaIngestor,
    ObjectStorage,
    object_storage_from_settings,
)
from kefu.messages.push import MessagePush, MessagePushWorker
from kefu.messages.receiver import MessageReceiver
from kefu.messages.recipients import RecipientResolver
from kefu.observability import database_target_fingerprint
from kefu.persistence.database import create_engine_from_url, create_session_factory
from kefu.relay.delivery import DeliveryWorker
from kefu.relay.handler import Relay
from kefu.wecom.deferred_reply import DeferredPassiveReplyStore
from kefu.wecom.transport import (
    FakeWeComAdapter,
    LongConnectionWeComAdapter,
    TransportDisplacedError,
    WeComTransport,
)
from kefu.workers.inbound import (
    _inbound_loop,
)
from kefu.workers.loops import (
    _delivery_loop,
    _group_issue_intake_cleanup_loop,
    _inspection_loop,
    _issue_intake_cleanup_loop,
    _issue_status_update_loop,
    _message_delivery_loop,
)

logger = logging.getLogger(__name__)


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
    media_ingestor = MediaIngestor(session_factory, storage)
    relay = Relay(
        session_factory,
        desk,
        media_ingestor=media_ingestor,
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
    issue_management = IssueManagement(session_factory, desk, settings, storage=storage)
    group_intake = GroupIssueIntakeService(
        session_factory,
        issue_management,
        status_agent,
        media_ingestor,
        storage,
    )
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
                tasks.create_task(_inspection_loop(inspection))
                tasks.create_task(_group_issue_intake_cleanup_loop(group_intake))
                tasks.create_task(_issue_intake_cleanup_loop(issue_management))
                tasks.create_task(
                    _inbound_loop(
                        relay=relay,
                        transport=transport,
                        receiver=message_receiver,
                        deferred_replies=deferred_replies,
                        group_intake=group_intake,
                        private_intake=issue_management,
                        media_ingestor=media_ingestor,
                        settings=settings,
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
    configure_logging("BOT-WORKER")
    asyncio.run(run())
