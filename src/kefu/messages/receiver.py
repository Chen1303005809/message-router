from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from kefu.messages.contracts import MessageSource, PushTarget, ReceivedMessage
from kefu.messages.push import EnqueuedMessage, MessagePush
from kefu.persistence.models import DeliveryDestination
from kefu.relay.handler import Relay, RelayDecision
from kefu.wecom.transport import InboundEvent


@dataclass(frozen=True, slots=True)
class InternalMessageResult:
    message: ReceivedMessage
    queued: EnqueuedMessage


class MessageReceiver:
    """Normalize external and internal message sources at one application seam."""

    def __init__(self, relay: Relay, push: MessagePush) -> None:
        self._relay = relay
        self._push = push

    def receive_wecom(self, event: InboundEvent, **options: object) -> RelayDecision:
        return self._relay.handle(event, **options)

    def receive_workbench(self, message_id: UUID, text: str) -> ReceivedMessage:
        return ReceivedMessage(
            source=MessageSource.WORKBENCH,
            message_id=str(message_id),
            text=text,
        )

    def receive_inspection(
        self,
        *,
        message_id: str,
        target: PushTarget,
        text: str,
        idempotency_key: str,
    ) -> InternalMessageResult:
        message = ReceivedMessage(
            source=MessageSource.INSPECTION,
            message_id=message_id,
            text=text,
            metadata={"destination_type": target.destination_type.value},
        )
        queued = self._push.enqueue(target, text, idempotency_key=idempotency_key)
        return InternalMessageResult(message=message, queued=queued)

    def receive_agent_push(
        self, *, message_id: str, destination_type: str, address: str, text: str
    ) -> InternalMessageResult:
        target = PushTarget(DeliveryDestination(destination_type), address)
        message = ReceivedMessage(
            source=MessageSource.AGENT,
            message_id=message_id,
            text=text,
            metadata={"destination_type": target.destination_type.value},
        )
        queued = self._push.enqueue(target, text, idempotency_key=f"agent:{message_id}")
        return InternalMessageResult(message=message, queued=queued)
