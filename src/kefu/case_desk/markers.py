"""Stable, non-secret event reference markers carried in robot messages."""

from __future__ import annotations

import re
import secrets

from kefu.case_desk.errors import ValidationError

# Crockford Base32 excludes I, L, O and U so short references are less easy to
# misread in a mobile chat.  References are random correlators, never grants.
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DEFAULT_CASE_REF_LENGTH = 7
_MARKER_RE = re.compile(r"〔\s*KF\s*[·.]\s*([0-9A-Za-z]+)\s*〕", re.IGNORECASE)


def new_case_ref(length: int = DEFAULT_CASE_REF_LENGTH) -> str:
    if length < 1:
        raise ValueError("case reference length must be positive")
    return "".join(secrets.choice(CROCKFORD_ALPHABET) for _ in range(length))


def normalize_case_ref(case_ref: str) -> str:
    normalized = case_ref.strip().upper()
    if len(normalized) != DEFAULT_CASE_REF_LENGTH or any(
        character not in CROCKFORD_ALPHABET for character in normalized
    ):
        raise ValidationError("事件引用标记格式无效")
    return normalized


def format_case_marker(case_ref: str) -> str:
    return f"〔KF·{normalize_case_ref(case_ref)}〕"


def parse_case_refs(content: str) -> frozenset[str]:
    """Extract only valid, case-insensitive reference markers from quote text."""
    refs: set[str] = set()
    for raw_ref in _MARKER_RE.findall(content):
        try:
            refs.add(normalize_case_ref(raw_ref))
        except ValidationError:
            continue
    return frozenset(refs)
