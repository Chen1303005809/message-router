"""Independent asynchronous loops hosted by the bot worker process."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from kefu.agent.status_worker import IssueStatusUpdateWorker
from kefu.inspection.schedule import next_inspection_at, scheduled_slot
from kefu.inspection.service import IssueInspectionService
from kefu.issues.group_intake import GroupIssueIntakeService
from kefu.issues.service import IssueManagement
from kefu.messages.push import MessagePushWorker
from kefu.relay.delivery import DeliveryWorker

logger = logging.getLogger(__name__)


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


async def _inspection_loop(inspection: IssueInspectionService) -> None:
    completed_slot: str | None = None
    while True:
        now = datetime.now(UTC)
        try:
            times = await asyncio.to_thread(inspection.daily_times)
            slot = scheduled_slot(now, times)
            if slot is not None and slot != completed_slot:
                result = await asyncio.to_thread(inspection.inspect_scheduled, now=now)
                completed_slot = slot
                if result.summary_messages or result.unresolved_targets:
                    logger.info(
                        "issue_inspection slot=%s scanned=%s summaries=%s unresolved=%s",
                        slot,
                        result.scanned_issues,
                        result.summary_messages,
                        result.unresolved_targets,
                    )
            if slot is not None:
                await asyncio.sleep(1)
                continue
            next_run = next_inspection_at(now, times)
            delay = max(0.1, (next_run - now).total_seconds())
            # Reload the schedule periodically so admin changes take effect
            # while this worker remains running.
            await asyncio.sleep(min(delay, 5.0))
        except Exception:
            logger.exception("issue_inspection_failed")
            await asyncio.sleep(1)


async def _group_issue_intake_cleanup_loop(intake: GroupIssueIntakeService) -> None:
    while True:
        try:
            removed = await intake.cleanup_expired()
            if removed:
                logger.info("expired group issue intake sessions removed=%s", removed)
        except Exception:
            logger.exception("group_issue_intake_cleanup_failed")
        await asyncio.sleep(60)


async def _issue_intake_cleanup_loop(issues: IssueManagement) -> None:
    while True:
        try:
            removed = await asyncio.to_thread(issues.cleanup_expired_drafts)
            if removed:
                logger.info("expired issue intake drafts and private handoffs removed=%s", removed)
        except Exception as error:
            logger.error(
                "issue_intake_cleanup_failed error_type=%s",
                type(error).__name__,
            )
        await asyncio.sleep(60)
