"""Sequential, durable delivery of already-recorded message bundles."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kefu.case_desk.contracts import (
    SYSTEM_ACTOR,
    DeliveryItemFailed,
    DeliveryItemSucceeded,
)
from kefu.case_desk.service import CaseDesk
from kefu.observability import opaque_id_fingerprint
from kefu.wecom.transport import OutboundMessage, WeComTransport

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryBatchResult:
    claimed_bundles: int
    sent_items: int
    failed_bundles: int


class DeliveryWorker:
    """Send one persisted bundle item at a time, preserving retry order."""

    def __init__(self, case_desk: CaseDesk, transport: WeComTransport) -> None:
        self._case_desk = case_desk
        self._transport = transport

    async def deliver_pending(self, limit: int = 20) -> DeliveryBatchResult:
        work_items = self._case_desk.claim_pending_deliveries(limit)
        sent_items = 0
        failed_bundles = 0
        for bundle in work_items:
            destination_fp = opaque_id_fingerprint(bundle.destination_address)
            for item in bundle.items:
                try:
                    receipt = await self._transport.send(
                        OutboundMessage(
                            destination_type=bundle.destination_type,
                            destination_address=bundle.destination_address,
                            req_id=item.req_id,
                            kind=item.kind,
                            payload=item.payload,
                        )
                    )
                except Exception as error:  # Transport errors must become durable state.
                    safe_error = (
                        str(error)
                        .replace(bundle.destination_address, f"<target:{destination_fp}>")
                        .replace("\n", " ")[:240]
                    )
                    logger.warning(
                        "[GROUP-TRACE] delivery_failed delivery_id=%s item_id=%s "
                        "req_id=%s case_ref=%s destination_type=%s destination_fp=%s error=%s",
                        bundle.id,
                        item.id,
                        item.req_id,
                        bundle.case_ref,
                        bundle.destination_type,
                        destination_fp,
                        safe_error or type(error).__name__,
                    )
                    self._case_desk.execute(
                        DeliveryItemFailed(
                            delivery_id=bundle.id,
                            item_id=item.id,
                            lock_token=bundle.lock_token,
                            error=str(error),
                        ),
                        SYSTEM_ACTOR,
                    )
                    failed_bundles += 1
                    break
                logger.info(
                    "[GROUP-TRACE] delivery_sent delivery_id=%s item_id=%s req_id=%s "
                    "case_ref=%s destination_type=%s destination_fp=%s",
                    bundle.id,
                    item.id,
                    item.req_id,
                    bundle.case_ref,
                    bundle.destination_type,
                    destination_fp,
                )
                self._case_desk.execute(
                    DeliveryItemSucceeded(
                        delivery_id=bundle.id,
                        item_id=item.id,
                        lock_token=bundle.lock_token,
                        platform_result=receipt.platform_result,
                    ),
                    SYSTEM_ACTOR,
                )
                sent_items += 1
        return DeliveryBatchResult(
            claimed_bundles=len(work_items), sent_items=sent_items, failed_bundles=failed_bundles
        )
