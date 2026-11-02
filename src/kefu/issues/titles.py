"""Canonical issue-title formatting shared by intake and case creation."""

from __future__ import annotations

_CUSTOMER_TITLE_SEPARATOR = " ｜ "


def format_customer_issue_title(
    issue_title: str,
    customer_name: str,
    *,
    previous_customer_name: str = "",
) -> str:
    """Prefix a recognized customer name once, keeping later drafts idempotent."""
    title = issue_title.strip()
    previous_customer = previous_customer_name.strip()
    if previous_customer:
        previous_prefix = f"{previous_customer}{_CUSTOMER_TITLE_SEPARATOR}"
        if title.startswith(previous_prefix):
            title = title[len(previous_prefix) :].lstrip()

    customer = customer_name.strip()
    if not title or not customer:
        return title

    prefix = f"{customer}{_CUSTOMER_TITLE_SEPARATOR}"
    suffix = f"{_CUSTOMER_TITLE_SEPARATOR}{customer}"
    if title.endswith(suffix):
        title = title[: -len(suffix)].rstrip()
    formatted = title if title.startswith(prefix) else f"{prefix}{title}"
    return formatted[:512]
