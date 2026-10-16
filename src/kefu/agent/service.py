from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

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
from kefu.config import Settings
from kefu.inspection.service import IssueInspectionService
from kefu.issues.contracts import IssueFields, IssueIntake
from kefu.issues.service import IssueManagement, map_image_placeholders
from kefu.messages.receiver import MessageReceiver
from kefu.messages.recipients import (
    RecipientKind,
    RecipientReference,
    RecipientResolver,
)
from kefu.persistence.models import CasePriority, CaseStatus, LifecycleStatus, WaitingOn

logger = logging.getLogger(__name__)

_AGENT_TIMEOUT_SECONDS = 60.0


class AgentConfigurationError(RuntimeError):
    pass


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


class Agent:
    """OpenAI Chat Completions adapter for issue drafting and authorized tool use."""

    def __init__(
        self,
        settings: Settings,
        tools: AgentToolRegistry | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = settings.agent_base_url
        self._api_key = settings.agent_api_key
        self._model = settings.agent_model
        self._tools = tools or AgentToolRegistry()
        self._client = client or httpx.AsyncClient(timeout=_AGENT_TIMEOUT_SECONDS)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def draft_issue(
        self,
        pasted_text: str,
        acceptance_groups: Sequence[str],
        *,
        request_id: str | None = None,
    ) -> dict[str, str]:
        if not pasted_text.strip():
            raise ValidationError("请先粘贴需要整理的问题内容")
        group_list = "、".join(acceptance_groups) if acceptance_groups else "无可用团队"
        prompt = (
            "根据咨询人员粘贴的企微聊天文本，生成问题草稿。只返回 JSON 对象，键为 "
            "issue_description、initial_acceptance_group、suspected_type、current_situation。"
            "initial_acceptance_group 必须从给定团队名中原样选择；无法判断时返回空字符串。"
            "suspected_type 无法判断时返回空字符串，不要发明枚举。"
            "图片占位符 [图片] 仅作为文本线索，"
            "不要推断图片内容。不要生成编号、过期时间、来源或受理人。\n\n"
            f"可选初次受理组：{group_list}\n\n粘贴文本：\n{pasted_text}"
        )
        result = await self._request_json(
            [
                {"role": "system", "content": "你负责整理问题资料，不得虚构事实。"},
                {"role": "user", "content": prompt},
            ],
            request_id=request_id,
            operation="draft_issue",
        )
        allowed = {
            "issue_description",
            "initial_acceptance_group",
            "suspected_type",
            "current_situation",
        }
        return {
            key: value.strip()
            for key, value in result.items()
            if key in allowed and isinstance(value, str)
        } | {key: "" for key in allowed if key not in result}

    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        actor: Actor,
        *,
        max_tool_rounds: int = 5,
        request_id: str | None = None,
    ) -> str:
        history: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "你是问题工作台 Agent。只处理用户有权访问的问题。需要读取或变更业务时调用工具；"
                    "不要伪造调用结果。问题确认和工作台初次推送必须由咨询人员操作。"
                ),
            }
        ]
        for item in messages:
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"}:
                raise ValidationError("Agent 对话仅接受 user 和 assistant 消息")
            if content is not None and not isinstance(content, str):
                raise ValidationError("Agent 对话内容必须是文本")
            history.append({"role": role, "content": content or ""})
        for _ in range(max_tool_rounds + 1):
            response = await self._post(
                {
                    "model": self._required_model(),
                    "messages": history,
                    "tools": self._tools.schemas,
                    "tool_choice": "auto",
                },
                request_id=request_id,
                operation="chat",
            )
            message = response["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = message.get("content")
                return content if isinstance(content, str) else ""
            history.append(message)
            for call in tool_calls:
                function = call.get("function", {})
                name = function.get("name")
                raw_arguments = function.get("arguments", "{}")
                try:
                    arguments = json.loads(raw_arguments)
                    result = self._tools.call(name, arguments, actor)
                    tool_content = json.dumps(_jsonable(result), ensure_ascii=False)
                except Exception as error:
                    tool_content = json.dumps({"error": str(error)}, ensure_ascii=False)
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": tool_content,
                    }
                )
        raise RuntimeError("Agent 工具调用超过最大轮数")

    async def _request_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        request_id: str | None = None,
        operation: str = "draft_issue",
    ) -> dict[str, str]:
        response = await self._post(
            {
                "model": self._required_model(),
                "messages": list(messages),
                "response_format": {"type": "json_object"},
            },
            request_id=request_id,
            operation=operation,
        )
        try:
            choices = response["choices"]
            message = choices[0]["message"]
            content = message.get("content") if isinstance(message, Mapping) else None
        except (IndexError, KeyError, TypeError) as error:
            logger.warning(
                "agent_response_parse_failed request_id=%s operation=%s failure=missing_message error_type=%s",
                _log_request_id(request_id),
                operation,
                type(error).__name__,
            )
            raise RuntimeError("Agent 服务返回了无效的 Chat Completions 响应") from error
        if not isinstance(content, str):
            logger.warning(
                "agent_response_parse_failed request_id=%s operation=%s failure=non_text_content",
                _log_request_id(request_id),
                operation,
            )
            raise RuntimeError("Agent 没有返回有效的 JSON 字段草稿")
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError) as error:
            logger.warning(
                "agent_response_parse_failed request_id=%s operation=%s failure=invalid_json_content content_chars=%s error_type=%s",
                _log_request_id(request_id),
                operation,
                len(content),
                type(error).__name__,
            )
            raise RuntimeError("Agent 没有返回有效的 JSON 字段草稿") from error
        if not isinstance(parsed, dict):
            logger.warning(
                "agent_response_parse_failed request_id=%s operation=%s failure=json_not_object",
                _log_request_id(request_id),
                operation,
            )
            raise RuntimeError("Agent 字段草稿必须是 JSON 对象")
        logger.info(
            "agent_response_parsed request_id=%s operation=%s field_count=%s",
            _log_request_id(request_id),
            operation,
            len(parsed),
        )
        return parsed

    async def _post(
        self,
        payload: Mapping[str, Any],
        *,
        request_id: str | None = None,
        operation: str = "agent",
    ) -> dict[str, Any]:
        if not self._base_url or not self._api_key:
            raise AgentConfigurationError("请配置 AGENT_BASE_URL 和 AGENT_API_KEY")
        url = _chat_completions_url(self._base_url)
        request_started = time.monotonic()
        endpoint_host, endpoint_path_depth, endpoint_has_query = _safe_endpoint_summary(url)
        payload_bytes = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        model = payload.get("model")
        logger.info(
            "agent_upstream_request_started request_id=%s operation=%s endpoint_host=%s endpoint_path_depth=%s endpoint_has_query=%s model=%s payload_bytes=%s timeout_seconds=%s",
            _log_request_id(request_id),
            operation,
            endpoint_host,
            endpoint_path_depth,
            endpoint_has_query,
            model if isinstance(model, str) else "unknown",
            payload_bytes,
            _AGENT_TIMEOUT_SECONDS,
        )
        try:
            response = await self._client.post(
                url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=dict(payload),
            )
        except httpx.TimeoutException as error:
            logger.warning(
                "agent_upstream_request_failed request_id=%s operation=%s failure=timeout error_type=%s elapsed_ms=%s",
                _log_request_id(request_id),
                operation,
                type(error).__name__,
                _elapsed_ms(request_started),
            )
            raise RuntimeError("Agent 服务请求失败") from error
        except httpx.InvalidURL as error:
            logger.error(
                "agent_upstream_request_failed request_id=%s operation=%s failure=invalid_url error_type=%s elapsed_ms=%s",
                _log_request_id(request_id),
                operation,
                type(error).__name__,
                _elapsed_ms(request_started),
            )
            raise RuntimeError("Agent 服务请求失败") from error
        except httpx.HTTPError as error:
            logger.warning(
                "agent_upstream_request_failed request_id=%s operation=%s failure=transport_error error_type=%s elapsed_ms=%s",
                _log_request_id(request_id),
                operation,
                type(error).__name__,
                _elapsed_ms(request_started),
            )
            raise RuntimeError("Agent 服务请求失败") from error

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            provider_error_type, provider_error_code = _upstream_error_labels(response)
            logger.warning(
                "agent_upstream_request_failed request_id=%s operation=%s failure=http_status status_code=%s provider_error_type=%s provider_error_code=%s content_type=%s response_bytes=%s upstream_request_id=%s elapsed_ms=%s",
                _log_request_id(request_id),
                operation,
                response.status_code,
                provider_error_type,
                provider_error_code,
                response.headers.get("content-type", "unknown"),
                len(response.content),
                _upstream_request_id(response),
                _elapsed_ms(request_started),
            )
            raise RuntimeError("Agent 服务请求失败") from error
        try:
            data = response.json()
        except (ValueError, httpx.DecodingError) as error:
            logger.warning(
                "agent_upstream_request_failed request_id=%s operation=%s failure=invalid_json status_code=%s content_type=%s response_bytes=%s upstream_request_id=%s elapsed_ms=%s error_type=%s",
                _log_request_id(request_id),
                operation,
                response.status_code,
                response.headers.get("content-type", "unknown"),
                len(response.content),
                _upstream_request_id(response),
                _elapsed_ms(request_started),
                type(error).__name__,
            )
            raise RuntimeError("Agent 服务返回了无效的 Chat Completions 响应") from error
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
            logger.warning(
                "agent_upstream_request_failed request_id=%s operation=%s failure=invalid_response_shape status_code=%s content_type=%s response_bytes=%s upstream_request_id=%s elapsed_ms=%s",
                _log_request_id(request_id),
                operation,
                response.status_code,
                response.headers.get("content-type", "unknown"),
                len(response.content),
                _upstream_request_id(response),
                _elapsed_ms(request_started),
            )
            raise RuntimeError("Agent 服务返回了无效的 Chat Completions 响应")
        logger.info(
            "agent_upstream_request_succeeded request_id=%s operation=%s status_code=%s content_type=%s response_bytes=%s upstream_request_id=%s elapsed_ms=%s",
            _log_request_id(request_id),
            operation,
            response.status_code,
            response.headers.get("content-type", "unknown"),
            len(response.content),
            _upstream_request_id(response),
            _elapsed_ms(request_started),
        )
        return data

    def _required_model(self) -> str:
        if not self._model:
            raise AgentConfigurationError("请配置 AGENT_MODEL")
        return self._model


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
        "基于已整理的文字创建问题草稿；问题确认和首次推送必须由咨询人员在工作台完成。",
        _object_schema(
            {
                "consult_queue_id": {"type": "string", "format": "uuid"},
                "initial_acceptance_team_id": {"type": "string", "format": "uuid"},
                "raw_text": {"type": "string", "minLength": 1},
                "issue_description": {"type": "string"},
                "suspected_type": {"type": "string"},
                "current_situation": {"type": "string"},
            },
            [
                "consult_queue_id",
                "initial_acceptance_team_id",
                "raw_text",
                "issue_description",
                "suspected_type",
                "current_situation",
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
                "waiting_on": {"type": "string", "enum": ["consult", "dev"]},
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
        "立即巡检所有未关闭问题；仅总管理员可以手动运行。",
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


def _execute_issue_operation(
    args: Mapping[str, Any], actor: Actor, issues: IssueManagement
) -> Any:
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
            waiting_on=WaitingOn(_string(args, "waiting_on")),
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
            approaching_window_minutes=_integer(
                args, "approaching_window_minutes", minimum=30
            ),
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
                issue_description=_string(args, "issue_description"),
                initial_acceptance_team_id=initial_team_id,
                suspected_type=_string(args, "suspected_type")
                if args.get("suspected_type")
                else "",
                current_situation=_string(args, "current_situation")
                if args.get("current_situation")
                else "",
            ),
        ),
        actor,
    )


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _safe_endpoint_summary(url: str) -> tuple[str, int, bool]:
    """Return endpoint diagnostics without logging URL credentials or query values."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "invalid", 0, False
    if not hostname:
        return "invalid", 0, bool(parsed.query)
    if ":" in hostname:
        hostname = f"[{hostname}]"
    endpoint_host = f"{parsed.scheme}://{hostname}"
    if port is not None:
        endpoint_host = f"{endpoint_host}:{port}"
    path_depth = sum(bool(part) for part in parsed.path.split("/"))
    return endpoint_host, path_depth, bool(parsed.query)


def _upstream_request_id(response: httpx.Response) -> str:
    for header_name in ("x-request-id", "openai-request-id", "request-id"):
        value = response.headers.get(header_name, "").strip()
        if value and len(value) <= 128 and all(
            char.isascii() and (char.isalnum() or char in "._:-") for char in value
        ):
            return value
    return "unavailable"


def _upstream_error_labels(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except (ValueError, httpx.DecodingError):
        return "unavailable", "unavailable"
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return "unavailable", "unavailable"
    return _safe_log_label(error.get("type")), _safe_log_label(error.get("code"))


def _safe_log_label(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 80:
        return "unavailable"
    normalized = "".join(
        char if char.isascii() and (char.isalnum() or char in "._:-") else "_"
        for char in value
    )
    return normalized or "unavailable"


def _log_request_id(request_id: str | None) -> str:
    normalized = (request_id or "").strip()
    if normalized and len(normalized) <= 64 and all(
        char.isascii() and (char.isalnum() or char in "_-") for char in normalized
    ):
        return normalized
    return "untracked"


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


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


def _integer(
    arguments: Mapping[str, Any], key: str, *, minimum: int | None = None
) -> int:
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
