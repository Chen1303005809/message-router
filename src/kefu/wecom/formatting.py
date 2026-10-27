"""Keep WeCom-facing text and marker formatting outside domain transitions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID, uuid4

from kefu.case_desk.markers import format_case_marker
from kefu.persistence.models import PartKind

_ANGLE_MENTION_RE = re.compile(r"<\s*[@＠][\w.-]+\s*>")
_AT_MENTION_RE = re.compile(r"(?<![\w.+-])[@＠][\w.-]+")


@dataclass(frozen=True, slots=True)
class SourcePart:
    kind: PartKind
    text: str | None = None
    media_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RenderedDeliveryItem:
    kind: PartKind
    payload: dict[str, object]


def build_formal_bundle(
    *,
    case_ref: str,
    bug_number: str,
    case_title: str,
    customer_name: str,
    speaker_name: str,
    parts: Iterable[SourcePart],
) -> tuple[RenderedDeliveryItem, ...]:
    """Render a decorated, quoteable message and its image attachments.

    The marker remains in ordinary message text for reliable reply association.
    """
    source_parts = tuple(parts)
    marker = format_case_marker(case_ref)
    text_parts = [
        _strip_at_mentions(source_part.text).strip()
        for source_part in source_parts
        if source_part.kind is PartKind.TEXT and source_part.text is not None
    ]
    if not text_parts or not any(part.strip() for part in text_parts):
        raise ValueError("正式消息至少要包含一段文字，才能生成普通消息")
    message_text = "\n".join(text_parts)
    content = format_case_message(
        case_title=case_title,
        body=message_text,
        details_after=(
            customer_name,
            speaker_name,
            bug_number,
            marker,
        ),
    )
    rendered: list[RenderedDeliveryItem] = [
        RenderedDeliveryItem(
            kind=PartKind.TEXT,
            payload={"content": content},
        )
    ]
    for source_part in source_parts:
        if source_part.kind is PartKind.IMAGE:
            assert source_part.media_id is not None
            rendered.append(
                RenderedDeliveryItem(
                    kind=PartKind.IMAGE,
                    payload={"media_id": str(source_part.media_id)},
                )
            )
    return tuple(rendered)


def _strip_at_mentions(text: str) -> str:
    """Remove visible WeCom @ mentions while preserving email addresses."""
    without_markup_mentions = _ANGLE_MENTION_RE.sub("", text)
    return _AT_MENTION_RE.sub("", without_markup_mentions)


def format_case_message(
    *,
    case_title: str,
    body: str,
    details_before: Iterable[str] = (),
    details_after: Iterable[str] = (),
) -> str:
    """Render a case message with the same title, body, and metadata hierarchy."""
    sections = [f"## {case_title}"]
    leading_details = tuple(details_before)
    if leading_details:
        sections.append("\n".join(f"> {detail}" for detail in leading_details))
    sections.append(f"**{body}**")
    trailing_details = tuple(details_after)
    if trailing_details:
        sections.append("\n".join(f"> {detail}" for detail in trailing_details))
    return "\n\n".join(sections)


def build_notice_bundle(
    *,
    case_ref: str,
    bug_number: str,
    case_title: str,
    operator_name: str,
    content: str,
    history_url: str,
) -> tuple[RenderedDeliveryItem, ...]:
    """Decorate a system notice and provide the internal history action."""
    marker = format_case_marker(case_ref)
    return (
        RenderedDeliveryItem(
            kind=PartKind.TEXT,
            payload={
                "content": (
                    format_case_message(
                        case_title=case_title,
                        body=content,
                        details_after=(
                            bug_number,
                            f"操作人：{operator_name}",
                            marker,
                        ),
                    )
                )
            },
        ),
        _history_card(
            case_ref, bug_number, case_title, operator_name, "操作人", history_url
        ),
    )


def _history_card(
    case_ref: str,
    bug_number: str,
    case_title: str,
    actor_name: str,
    actor_label: str,
    history_url: str,
) -> RenderedDeliveryItem:
    marker = format_case_marker(case_ref)
    action = {"type": 1, "title": "查看问题历史", "url": history_url}
    return RenderedDeliveryItem(
        kind=PartKind.TEXT,
        payload={
            "template_card": {
                "card_type": "text_notice",
                "main_title": {
                    "title": case_title[:26],
                    "desc": f"{actor_label}：{actor_name}"[:30],
                },
                "sub_title_text": (
                    f"{bug_number} · {marker} · 点击查看完整问题时间线"
                ),
                "jump_list": [action],
                "card_action": {"type": 1, "url": history_url},
                "task_id": f"kefu_history_{uuid4().hex}",
            }
        },
    )
