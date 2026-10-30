"""Dependencies shared by the API route modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from kefu.admin.service import Administration
from kefu.agent.service import Agent, AgentToolRegistry
from kefu.case_desk.service import CaseDesk
from kefu.config import Settings
from kefu.inspection.service import IssueInspectionService
from kefu.issues.service import IssueManagement
from kefu.media.storage import MediaIngestor, ObjectStorage
from kefu.messages.push import MessagePush
from kefu.messages.receiver import MessageReceiver
from kefu.messages.recipients import RecipientResolver
from kefu.web.auth import WebAuthenticator
from kefu.wecom.customer_groups import WeComCustomerGroupClient


@dataclass(slots=True)
class WebRuntime:
    settings: Settings
    session_factory: Callable[[], Session]
    authenticator: WebAuthenticator
    storage: ObjectStorage
    wecom_customer_groups: WeComCustomerGroupClient
    desk: CaseDesk
    administration: Administration
    media_ingestor: MediaIngestor
    issue_management: IssueManagement
    message_push: MessagePush
    relay: Any
    message_receiver: MessageReceiver
    recipient_resolver: RecipientResolver
    inspection: IssueInspectionService
    agent_tools: AgentToolRegistry
    agent: Agent
    external_path: Callable[[str], str]
    actor_or_error: Callable[[Any], Any]
