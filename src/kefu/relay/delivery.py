"""Sequential, durable delivery of already-recorded message bundles."""

from __future__ import annotations

from dataclasses import dataclass

from kefu.case_desk.contracts import (
    SYSTEM_ACTOR,
    DeliveryItemFailed,
    DeliveryItemSucceeded,
)
from kefu.case_desk.service import CaseDesk
from kefu.wecom.transport import OutboundMessage, WeComTransport


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
