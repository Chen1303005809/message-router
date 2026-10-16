"""Logged synchronous and asynchronous HTTP requests for outbound services."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit
from uuid import uuid4

import httpx

_LOGGER = logging.getLogger(__name__)


class LoggedHttpClient:
    """Send HTTP requests with consistent timing and safe diagnostics.

    A caller may inject a long-lived httpx client for connection reuse. Without one, each
    call uses and closes a short-lived client configured with this wrapper's timeout.
    """

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        client: httpx.Client | httpx.AsyncClient | None = None,
    ) -> None:
        self._timeout = timeout
        self._client = client

    def request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        request_id: str | None = None,
        raise_for_status: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        if isinstance(self._client, httpx.AsyncClient):
            raise TypeError("同步请求不能使用 httpx.AsyncClient")
        if self._client is not None:
            return self._request(
                self._client, method, url, operation, request_id, kwargs, raise_for_status
            )
        with httpx.Client(timeout=self._timeout) as client:
            return self._request(
                client, method, url, operation, request_id, kwargs, raise_for_status
            )

    async def arequest(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        request_id: str | None = None,
        raise_for_status: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        if isinstance(self._client, httpx.Client):
            raise TypeError("异步请求不能使用 httpx.Client")
        if self._client is not None:
            return await self._arequest(
                self._client, method, url, operation, request_id, kwargs, raise_for_status
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._arequest(
                client, method, url, operation, request_id, kwargs, raise_for_status
            )

    def _request(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        operation: str,
        request_id: str | None,
        kwargs: Mapping[str, Any],
        raise_for_status: bool,
    ) -> httpx.Response:
        correlation_id, started_at = self._log_started(
            method, url, operation, request_id, kwargs
        )
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TimeoutException as error:
            self._log_transport_failure(correlation_id, operation, "timeout", error, started_at)
            raise
        except httpx.InvalidURL as error:
            self._log_transport_failure(correlation_id, operation, "invalid_url", error, started_at)
            raise
        except httpx.HTTPError as error:
            self._log_transport_failure(
                correlation_id, operation, "transport_error", error, started_at
            )
            raise
        self._log_response(correlation_id, operation, response, url, kwargs, started_at)
        if raise_for_status:
            response.raise_for_status()
        return response

    async def _arequest(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        operation: str,
        request_id: str | None,
        kwargs: Mapping[str, Any],
        raise_for_status: bool,
    ) -> httpx.Response:
        correlation_id, started_at = self._log_started(
            method, url, operation, request_id, kwargs
        )
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as error:
            self._log_transport_failure(correlation_id, operation, "timeout", error, started_at)
            raise
        except httpx.InvalidURL as error:
            self._log_transport_failure(correlation_id, operation, "invalid_url", error, started_at)
            raise
        except httpx.HTTPError as error:
            self._log_transport_failure(
                correlation_id, operation, "transport_error", error, started_at
            )
            raise
        self._log_response(correlation_id, operation, response, url, kwargs, started_at)
        if raise_for_status:
            response.raise_for_status()
        return response

    def _log_started(
        self,
        method: str,
        url: str,
        operation: str,
        request_id: str | None,
        kwargs: Mapping[str, Any],
    ) -> tuple[str, float]:
        started_at = time.monotonic()
        correlation_id = _request_id(request_id)
        endpoint_host, path_depth, has_query = _endpoint_summary(url)
        has_query = has_query or kwargs.get("params") is not None
        _LOGGER.info(
            "outbound_http_request_started request_id=%s operation=%s method=%s "
            "endpoint_host=%s endpoint_path_depth=%s endpoint_has_query=%s "
            "request_bytes=%s timeout_seconds=%s",
            correlation_id,
            _log_label(operation),
            _log_method(method),
            endpoint_host,
            path_depth,
            has_query,
            _request_bytes(kwargs),
            _timeout_seconds(kwargs.get("timeout", self._timeout)),
        )
        return correlation_id, started_at

    @staticmethod
    def _log_transport_failure(
        request_id: str,
        operation: str,
        failure: str,
        error: httpx.HTTPError,
        started_at: float,
    ) -> None:
        log = _LOGGER.error if failure == "invalid_url" else _LOGGER.warning
        log(
            "outbound_http_request_failed request_id=%s operation=%s failure=%s "
            "error_type=%s elapsed_ms=%s",
            request_id,
            _log_label(operation),
            failure,
            type(error).__name__,
            _elapsed_ms(started_at),
        )

    @staticmethod
    def _log_response(
        request_id: str,
        operation: str,
        response: httpx.Response,
        url: str,
        request_kwargs: Mapping[str, Any],
        started_at: float,
    ) -> None:
        common = (
            request_id,
            _log_label(operation),
            response.status_code,
            _content_type(response),
            len(response.content),
            _upstream_request_id(response),
            _elapsed_ms(started_at),
        )
        if response.is_error:
            error_type, error_code, error_param, category, message = _error_diagnostics(
                response, url, request_kwargs
            )
            _LOGGER.warning(
                "outbound_http_request_failed request_id=%s operation=%s failure=http_status "
                "status_code=%s provider_error_type=%s provider_error_code=%s "
                "provider_error_param=%s provider_error_category=%s "
                "provider_error_message=%r content_type=%s response_bytes=%s "
                "upstream_request_id=%s elapsed_ms=%s",
                common[0],
                common[1],
                common[2],
                error_type,
                error_code,
                error_param,
                category,
                message,
                common[3],
                common[4],
                common[5],
                common[6],
            )
            return
        _LOGGER.info(
            "outbound_http_request_succeeded request_id=%s operation=%s status_code=%s "
            "content_type=%s response_bytes=%s upstream_request_id=%s elapsed_ms=%s",
            *common,
        )


def _endpoint_summary(url: str) -> tuple[str, int, bool]:
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


def _request_bytes(kwargs: Mapping[str, Any]) -> int | str:
    if "json" in kwargs:
        try:
            return len(json.dumps(kwargs["json"], ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return "unknown"
    for key in ("content", "data"):
        value = kwargs.get(key)
        if isinstance(value, str):
            return len(value.encode("utf-8"))
        if isinstance(value, bytes):
            return len(value)
        if isinstance(value, Mapping):
            try:
                return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError):
                return "unknown"
    return 0


def _timeout_seconds(timeout: Any) -> float | str:
    if isinstance(timeout, httpx.Timeout):
        values = (timeout.connect, timeout.read, timeout.write, timeout.pool)
        numbers = [value for value in values if value is not None]
        return max(numbers) if numbers else "unlimited"
    if isinstance(timeout, (int, float)):
        return float(timeout)
    return "client_default"


def _content_type(response: httpx.Response) -> str:
    value = response.headers.get("content-type", "unknown")
    return " ".join(value.split())[:128]


def _upstream_request_id(response: httpx.Response) -> str:
    for header_name in ("x-request-id", "openai-request-id", "request-id"):
        value = response.headers.get(header_name, "").strip()
        if value and len(value) <= 128 and all(
            char.isascii() and (char.isalnum() or char in "._:-") for char in value
        ):
            return value
    return "unavailable"


def _error_diagnostics(
    response: httpx.Response,
    url: str,
    request_kwargs: Mapping[str, Any],
) -> tuple[str, str, str, str, str]:
    try:
        response_payload = response.json()
    except (ValueError, httpx.DecodingError):
        return "unavailable", "unavailable", "unavailable", "unclassified", "unavailable"
    error = response_payload.get("error") if isinstance(response_payload, dict) else None
    if not isinstance(error, dict) and isinstance(response_payload, dict):
        error = response_payload
    if not isinstance(error, dict):
        return "unavailable", "unavailable", "unavailable", "unclassified", "unavailable"
    error_type = _safe_label(error.get("type"))
    error_code = _safe_label(error.get("code", error.get("errcode")))
    error_param = _safe_label(error.get("param"), parameter=True)
    message_value = error.get("message", error.get("errmsg"))
    message = _safe_error_message(message_value, url, request_kwargs)
    category = _error_category(message_value, error_param)
    return error_type, error_code, error_param, category, message


def _safe_label(value: Any, *, parameter: bool = False) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value or len(value) > 80:
        return "unavailable"
    allowed_punctuation = ".[]" if parameter else "._:-"
    if all(char.isascii() and (char.isalnum() or char in allowed_punctuation) for char in value):
        return value
    return "unavailable"


def _error_category(message: Any, error_param: str) -> str:
    if not isinstance(message, str):
        return "unclassified"
    normalized = message.casefold()
    if "response_format" in normalized or "json_object" in normalized:
        return "response_format"
    if any(
        phrase in normalized
        for phrase in ("context length", "maximum context", "too many tokens", "token limit")
    ):
        return "context_limit"
    if "model" in normalized and any(
        phrase in normalized for phrase in ("not found", "unsupported", "invalid", "does not exist")
    ):
        return "model"
    if error_param != "unavailable":
        return "invalid_parameter"
    if "endpoint" in normalized or "route" in normalized or "path" in normalized:
        return "endpoint"
    if "unsupported" in normalized or "not supported" in normalized:
        return "unsupported_feature"
    return "unclassified"


def _safe_error_message(message: Any, url: str, request_kwargs: Mapping[str, Any]) -> str:
    if not isinstance(message, str) or not message.strip():
        return "unavailable"
    normalized = " ".join(message.split())
    for candidate in _sensitive_values(url, request_kwargs):
        candidate = " ".join(candidate.split())
        if len(candidate) >= 4 and candidate in normalized:
            normalized = normalized.replace(candidate, "[redacted]")
        if len(candidate) < 8:
            continue
        window_length = min(12, len(candidate))
        window_step = max(1, window_length // 2)
        final_offset = len(candidate) - window_length
        if any(
            candidate[offset : offset + window_length] in normalized
            for offset in range(0, final_offset + 1, window_step)
        ) or candidate[final_offset:] in normalized:
            return "[redacted: request content may be echoed]"
    return normalized[:240]


def _sensitive_values(url: str, request_kwargs: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    try:
        parsed = urlsplit(url)
        values.extend(value for _, value in parse_qsl(parsed.query, keep_blank_values=True))
        values.extend(
            unquote(value)
            for value in (parsed.username, parsed.password)
            if value is not None
        )
    except ValueError:
        pass
    _collect_strings(request_kwargs.get("json"), values)
    _collect_strings(request_kwargs.get("data"), values)
    _collect_strings(request_kwargs.get("content"), values, include_short=True)
    parameters = request_kwargs.get("params")
    if isinstance(parameters, Mapping):
        for value in parameters.values():
            _collect_strings(value, values, include_short=True)
    elif isinstance(parameters, (list, tuple)):
        for item in parameters:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                _collect_strings(item[1], values, include_short=True)
    elif parameters is not None:
        multi_items = getattr(parameters, "multi_items", None)
        if callable(multi_items):
            for _, value in multi_items():
                _collect_strings(value, values, include_short=True)
    headers = request_kwargs.get("headers")
    if isinstance(headers, Mapping):
        for name, value in headers.items():
            normalized_name = str(name).casefold()
            if any(
                marker in normalized_name
                for marker in (
                    "authorization",
                    "token",
                    "secret",
                    "api-key",
                    "api_key",
                    "cookie",
                )
            ):
                _collect_strings(value, values, include_short=True)
    return tuple(dict.fromkeys(value for value in values if value))


def _collect_strings(
    value: Any,
    result: list[str],
    depth: int = 0,
    *,
    include_short: bool = False,
) -> None:
    if depth > 12:
        return
    if isinstance(value, str):
        if include_short or len(value) >= 24:
            result.append(value)
    elif isinstance(value, bytes):
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError:
            return
        if include_short or len(decoded) >= 24:
            result.append(decoded)
    elif isinstance(value, Mapping):
        sensitive_fields = {
            "content",
            "prompt",
            "raw_text",
            "text",
            "body",
            "input",
            "message",
            "password",
            "secret",
            "token",
            "api_key",
            "authorization",
        }
        for key, item in value.items():
            normalized_key = str(key).casefold()
            field_is_sensitive = normalized_key in sensitive_fields or any(
                marker in normalized_key
                for marker in ("password", "secret", "token", "api_key", "authorization")
            )
            _collect_strings(
                item,
                result,
                depth + 1,
                include_short=include_short or field_is_sensitive,
            )
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_strings(item, result, depth + 1, include_short=include_short)


def _request_id(value: str | None) -> str:
    normalized = (value or "").strip()
    if normalized and len(normalized) <= 64 and all(
        char.isascii() and (char.isalnum() or char in "_-") for char in normalized
    ):
        return normalized
    return uuid4().hex


def _log_label(value: str) -> str:
    normalized = value.strip()
    if normalized and len(normalized) <= 64 and all(
        char.isascii() and (char.isalnum() or char in "_.:-") for char in normalized
    ):
        return normalized
    return "unknown"


def _log_method(value: str) -> str:
    normalized = value.upper()
    if normalized and len(normalized) <= 16 and normalized.isascii() and normalized.isalpha():
        return normalized
    return "unknown"


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))
