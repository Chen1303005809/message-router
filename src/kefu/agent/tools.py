"""Agent function tools and their argument handling."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from kefu.case_desk.contracts import (
    Actor,
    AdjustCaseDeadline,
    CaseFilter,
    CloseCase,
    ExtendCaseDeadline,
    MessageIntent,
    PostFormalMessage,
    ReopenCase,
    SetCaseApproachingWindow,
    SetCaseStatus,
    TextPart,
    TransferConsultant,
    TransferDevTeam,
    UpdateCaseMetadata,
)
from kefu.case_desk.errors import ValidationError
from kefu.inspection.service import IssueInspectionService
from kefu.issues.contracts import IssueFields, IssueIntake
from kefu.issues.service import IssueManagement, map_image_placeholders
from kefu.messages.receiver import MessageReceiver
from kefu.messages.recipients import (
    RecipientKind,
    RecipientReference,
    RecipientResolver,
)
from kefu.persistence.models import (
    CasePriority,
    CaseStatus,
    LifecycleStatus,
    WaitingOn,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentTool:
    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any], Actor], Any]

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


class AgentToolRegistry:
    """Register the same authorized functions used by programs as OpenAI tools."""

    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: Mapping[str, Any],
        handler: Callable[[Mapping[str, Any], Actor], Any],
    ) -> None:
        if not name or name in self._tools:
            raise ValueError("Agent 工具名称必须唯一且不能为空")
        self._tools[name] = AgentTool(name, description, parameters, handler)

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.openai_schema() for tool in self._tools.values()]

    def call(self, name: str, arguments: Mapping[str, Any], actor: Actor) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ValidationError("Agent 调用了未注册的工具")
        if not isinstance(arguments, Mapping):
            raise ValidationError("Agent 工具参数必须是对象")
        return tool.handler(arguments, actor)


def build_tool_registry(
    issues: IssueManagement,
    inspection: IssueInspectionService,
    receiver: MessageReceiver,
    recipients: RecipientResolver,
) -> AgentToolRegistry:
    registry = AgentToolRegistry()
    registry.register(
        "issue.get",
        "读取当前用户有权查看的问题及其沟通时间线。",
        _object_schema({"issue_number": {"type": "string"}}, ["issue_number"]),
        lambda args, actor: issues.get(_string(args, "issue_number"), actor),
    )
    registry.register(
        "issue.create_intake_draft",
        "基于已整理的文字创建问题草稿，提取明确出现的客户信息且不要猜测；"
        "问题确认和首次推送必须由咨询人员在工作台完成。",
        _object_schema(
            {
                "consult_queue_id": {"type": "string", "format": "uuid"},
                "initial_acceptance_team_id": {"type": "string", "format": "uuid"},
                "raw_text": {"type": "string", "minLength": 1},
                "issue_title": {"type": "string", "maxLength": 512},
                "issue_description": {"type": "string"},
                "suspected_type": {"type": "string"},
                "current_situation": {"type": "string"},
                "customer_name": {"type": "string", "maxLength": 256},
                "customer_contact_name": {"type": "string", "maxLength": 256},
                "customer_contact_method": {"type": "string", "maxLength": 256},
            },
            [
                "consult_queue_id",
                "initial_acceptance_team_id",
                "raw_text",
                "issue_title",
                "issue_description",
                "suspected_type",
                "current_situation",
                "customer_name",
            ],
        ),
        lambda args, actor: _create_issue_draft(args, actor, issues),
    )
    registry.register(
        "issue.list",
        "列出当前用户有权查看的未关闭问题。",
        _object_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
        lambda args, actor: issues.list(
            CaseFilter(lifecycle_status=LifecycleStatus.OPEN, page_size=int(args.get("limit", 20))),
            actor,
        ),
    )
    registry.register(
        "issue.execute",
        (
            "执行当前用户有权进行的问题跟进操作。先读取问题以获取最新 version；"
            "所有权限由问题管理模块校验。"
        ),
        _object_schema(
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "post_message",
                        "close",
                        "reopen",
                        "set_status",
                        "adjust_deadline",
                        "extend_deadline",
                        "set_approaching_window",
                        "transfer_consultant",
                        "transfer_dev_team",
                        "update_metadata",
                    ],
                },
                "issue_number": {"type": "string"},
                "version": {"type": "integer", "minimum": 1},
                "text": {"type": "string"},
                "intent": {"type": "string", "enum": ["handoff", "sync"]},
                "status": {
                    "type": "string",
                    "enum": [
                        "pending_confirmation",
                        "in_progress",
                        "waiting_customer",
                        "suspended",
                    ],
                },
                "adjustment_minutes": {"type": "integer"},
                "extension_minutes": {"type": "integer", "minimum": 30},
                "approaching_window_minutes": {"type": "integer", "minimum": 30},
                "new_consultant_id": {"type": "string", "format": "uuid"},
                "new_dev_team_id": {"type": "string", "format": "uuid"},
                "customer_name": {"type": "string"},
                "customer_group_name": {"type": ["string", "null"], "maxLength": 256},
                "customer_contact_name": {"type": ["string", "null"]},
                "customer_contact_method": {"type": ["string", "null"]},
                "priority": {"type": "string", "enum": ["normal", "urgent", "severe"]},
            },
            ["action", "issue_number", "version"],
        ),
        lambda args, actor: _execute_issue_operation(args, actor, issues),
    )
    registry.register(
        "issue.receive_workbench_message",
        "将工作台文本规范化为统一接收消息。图片文件不会传入此工具。",
        _object_schema(
            {"message_id": {"type": "string"}, "text": {"type": "string"}}, ["message_id", "text"]
        ),
        lambda args, _actor: receiver.receive_workbench(
            UUID(_string(args, "message_id")), _string(args, "text")
        ),
    )
    registry.register(
        "inspection.run",
        "立即按当前研发责任团队汇总所有未关闭问题；仅总管理员可以手动运行。",
        _object_schema({}, []),
        lambda _args, actor: inspection.inspect(actor=actor),
    )
    registry.register(
        "message.push",
        "向登记过的企微团队群聊或个人发送文本消息。",
        _object_schema(
            {
                "recipient_kind": {"type": "string", "enum": ["team", "consult_queue", "person"]},
                "recipient_id": {"type": "string", "format": "uuid"},
                "text": {"type": "string", "minLength": 1},
            },
            ["recipient_kind", "recipient_id", "text"],
        ),
        lambda args, actor: _agent_push(args, actor, receiver, recipients),
    )
    return registry


def _agent_push(
    args: Mapping[str, Any],
    actor: Actor,
    receiver: MessageReceiver,
    recipients: RecipientResolver,
) -> Any:
    try:
        kind = RecipientKind(_string(args, "recipient_kind"))
        recipient_id = UUID(_string(args, "recipient_id"))
    except ValueError as error:
        raise ValidationError("消息接收目标无效") from error
    target = recipients.authorize(actor, RecipientReference(kind, recipient_id))
    return receiver.receive_agent_push(
        message_id=str(uuid4()),
        destination_type=target.destination_type.value,
        address=target.address,
        text=_string(args, "text"),
    )


def _execute_issue_operation(args: Mapping[str, Any], actor: Actor, issues: IssueManagement) -> Any:
    action = _string(args, "action")
    issue_number = _string(args, "issue_number")
    version = _integer(args, "version", minimum=1)
    if action == "post_message":
        intent = MessageIntent(args.get("intent", MessageIntent.HANDOFF.value))
        command = PostFormalMessage(
            case_ref=issue_number,
            expected_version=version,
            parts=(TextPart(_string(args, "text")),),
            intent=intent,
        )
    elif action == "close":
        command = CloseCase(case_ref=issue_number, expected_version=version)
    elif action == "reopen":
        command = ReopenCase(
            case_ref=issue_number,
            expected_version=version,
            waiting_on=WaitingOn.DEV,
        )
    elif action == "set_status":
        command = SetCaseStatus(
            case_ref=issue_number,
            expected_version=version,
            status=CaseStatus(_string(args, "status")),
        )
    elif action == "adjust_deadline":
        command = AdjustCaseDeadline(
            case_ref=issue_number,
            expected_version=version,
            adjustment_minutes=_integer(args, "adjustment_minutes"),
        )
    elif action == "extend_deadline":
        command = ExtendCaseDeadline(
            case_ref=issue_number,
            expected_version=version,
            extension_minutes=_integer(args, "extension_minutes", minimum=30),
        )
    elif action == "set_approaching_window":
        command = SetCaseApproachingWindow(
            case_ref=issue_number,
            expected_version=version,
            approaching_window_minutes=_integer(args, "approaching_window_minutes", minimum=30),
        )
    elif action == "transfer_consultant":
        command = TransferConsultant(
            case_ref=issue_number,
            expected_version=version,
            new_consultant_id=_uuid(args, "new_consultant_id"),
        )
    elif action == "transfer_dev_team":
        command = TransferDevTeam(
            case_ref=issue_number,
            expected_version=version,
            new_dev_team_id=_uuid(args, "new_dev_team_id"),
        )
    elif action == "update_metadata":
        command = UpdateCaseMetadata(
            case_ref=issue_number,
            expected_version=version,
            customer_name=_string(args, "customer_name"),
            customer_group_name=_optional_string(args.get("customer_group_name")),
            customer_contact_name=_optional_string(args.get("customer_contact_name")),
            customer_contact_method=_optional_string(args.get("customer_contact_method")),
            priority=CasePriority(_string(args, "priority")),
        )
    else:  # pragma: no cover - enum schema and this guard share the same action set.
        raise ValidationError("Agent 请求了不支持的问题操作")
    return issues.execute(command, actor)


def _create_issue_draft(args: Mapping[str, Any], actor: Actor, issues: IssueManagement) -> Any:
    try:
        consult_queue_id = UUID(_string(args, "consult_queue_id"))
        initial_team_id = UUID(_string(args, "initial_acceptance_team_id"))
    except ValueError as error:
        raise ValidationError("问题草稿中的团队标识无效") from error
    raw_text = _string(args, "raw_text")
    return issues.create_intake_draft(
        IssueIntake(
            consult_queue_id=consult_queue_id,
            raw_text=raw_text,
            parts=map_image_placeholders(raw_text, ()),
            fields=IssueFields(
                issue_title=_string(args, "issue_title"),
                issue_description=_string(args, "issue_description"),
                initial_acceptance_team_id=initial_team_id,
                suspected_type=_string(args, "suspected_type")
                if args.get("suspected_type")
                else "",
                current_situation=_string(args, "current_situation")
                if args.get("current_situation")
                else "",
                customer_name=(_string(args, "customer_name") if args.get("customer_name") else ""),
                customer_contact_name=(
                    _string(args, "customer_contact_name")
                    if args.get("customer_contact_name")
                    else ""
                ),
                customer_contact_method=(
                    _string(args, "customer_contact_method")
                    if args.get("customer_contact_method")
                    else ""
                ),
            ),
        ),
        actor,
    )


def _object_schema(properties: Mapping[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _string(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{key} 不能为空")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("可选文本字段必须是字符串")
    return value.strip() or None


def _integer(arguments: Mapping[str, Any], key: str, *, minimum: int | None = None) -> int:
    value = arguments.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{key} 必须是整数")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{key} 不能小于 {minimum}")
    return value


def _uuid(arguments: Mapping[str, Any], key: str) -> UUID:
    try:
        return UUID(_string(arguments, key))
    except ValueError as error:
        raise ValidationError(f"{key} 必须是有效的 UUID") from error


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {
            ("issue_number" if key == "case_ref" else str(key)): _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)
