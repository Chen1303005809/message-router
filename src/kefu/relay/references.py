"""Strict quote parsing: ambiguous or absent markers must never be forwarded."""

from __future__ import annotations

from kefu.case_desk.errors import ValidationError
from kefu.case_desk.markers import parse_case_refs


def parse_quoted_case_ref(quote_content: str) -> str:
    refs = parse_case_refs(quote_content)
    if not refs:
        raise ValidationError("引用内容中没有有效事件标记，请引用机器人发送的文字片段")
    if len(refs) != 1:
        raise ValidationError("引用内容包含多个事件标记，无法唯一归类")
    return next(iter(refs))
