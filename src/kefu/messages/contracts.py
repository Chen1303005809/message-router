from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kefu.persistence.models import DeliveryDestination


class MessageSource(StrEnum):
    WECOM = "wecom"
    WORKBENCH = "workbench"
    INSPECTION = "inspection"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class PushTarget:
    destination_type: DeliveryDestination
    address: str

    def __post_init__(self) -> None:
        if not self.address.strip():
            raise ValueError("消息投递目标不能为空")


@dataclass(frozen=True, slots=True)
class ReceivedMessage:
    source: MessageSource
    message_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
