"""Consistent, feature-labelled logging for the web and worker processes."""

from __future__ import annotations

import copy
import logging

_LOGGER_FEATURES = {
    "kefu.admin.service": "ADMIN",
    "kefu.agent.status_worker": "ISSUE-STATUS",
    "kefu.issues.group_intake": "GROUP-INTAKE",
    "kefu.issues.service": "ISSUE-SERVICE",
    "kefu.messages.push": "MESSAGE-PUSH",
    "kefu.relay.delivery": "MESSAGE-DELIVERY",
    "kefu.relay.handler": "GROUP-RELAY",
    "kefu.routing.directory": "ROUTING",
    "kefu.tools.http": "OUTBOUND-HTTP",
    "kefu.wecom.customer_groups": "WECOM-CUSTOMER-GROUP",
    "kefu.wecom.transport": "WECOM-TRANSPORT",
    "kefu.worker": "WORKER",
    "uvicorn.access": "HTTP-ACCESS",
    "uvicorn.error": "WEB-SERVER",
    "httpx": "HTTP-CLIENT",
}
_OPERATION_FEATURES = {
    "chat": "AI-CHAT",
    "classify_issue_status": "ISSUE-STATUS-AI",
    "draft_group_issue": "GROUP-ISSUE-AI",
    "draft_issue": "ISSUE-DRAFT-AI",
    "wecom_agent_jsapi_ticket": "WECOM-JS-SDK",
    "wecom_customer_group_get": "WECOM-CUSTOMER-GROUP",
    "wecom_customer_group_gettoken": "WECOM-CUSTOMER-GROUP",
    "wecom_oauth_gettoken": "WECOM-OAUTH",
    "wecom_oauth_userinfo": "WECOM-OAUTH",
}


def _operation_feature(record: logging.LogRecord) -> str | None:
    args = record.args
    if isinstance(args, tuple) and len(args) > 1 and isinstance(args[1], str):
        return _OPERATION_FEATURES.get(args[1])
    return None


def _feature_for(record: logging.LogRecord) -> str:
    message = record.msg if isinstance(record.msg, str) else ""
    if message.startswith("[GROUP-TRACE]"):
        return "GROUP-TRACE"
    if message.startswith("[INBOUND-DIAG]"):
        return "INBOUND-DIAG"

    if record.name == "kefu.web.app":
        if message.startswith("issue_draft_"):
            return "ISSUE-DRAFT"
        if message.startswith("agent_http_request_"):
            return "WEB-REQUEST"
        return "WEB"
    if record.name == "kefu.worker":
        if message.startswith("processed delivery bundles"):
            return "MESSAGE-DELIVERY"
        if message.startswith("processed standalone messages"):
            return "MESSAGE-PUSH"
        if message.startswith(("processed issue status", "issue_status_update_")):
            return "ISSUE-STATUS"
        if message.startswith("issue_inspection_"):
            return "ISSUE-INSPECTION"
        if message.startswith(
            (
                "expired group issue intake sessions",
                "group_issue_intake_",
            )
        ):
            return "GROUP-INTAKE"
    if record.name in {"kefu.agent.service", "kefu.tools.http", "kefu.wecom.customer_groups"}:
        operation_feature = _operation_feature(record)
        if operation_feature:
            return operation_feature
        if record.name == "kefu.agent.service":
            return "AI-AGENT"
        if record.name == "kefu.wecom.customer_groups":
            return "WECOM-CUSTOMER-GROUP"
        return "OUTBOUND-HTTP"

    for logger_name, feature in _LOGGER_FEATURES.items():
        if record.name == logger_name or record.name.startswith(f"{logger_name}."):
            return feature
    if record.name.startswith("kefu."):
        return record.name.removeprefix("kefu.").split(".", 1)[0].upper()
    return "APP"


class FeatureLogFormatter(logging.Formatter):
    """Add a visible service and feature boundary to every log record."""

    def __init__(self, service: str) -> None:
        super().__init__(
            fmt=(
                "%(asctime)s %(levelname)s ===[%(log_service)s / %(log_feature)s]=== "
                "%(name)s | %(message)s"
            )
        )
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        formatted_record = copy.copy(record)
        formatted_record.log_service = self.service
        formatted_record.log_feature = _feature_for(record)
        return super().format(formatted_record)


def configure_logging(service: str) -> None:
    """Install a common feature-labelled formatter without replacing handlers."""
    logging.basicConfig(level=logging.INFO)
    formatter = FeatureLogFormatter(service)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in root.handlers:
        handler.setFormatter(formatter)

    # Uvicorn configures its access and error handlers before importing the app.
    # Update those handlers too so access records use the same visible labels.
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        for handler in logging.getLogger(logger_name).handlers:
            handler.setFormatter(formatter)
