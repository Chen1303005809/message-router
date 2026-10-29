"""Daily Shanghai-time schedule for issue inspection."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from kefu.case_desk.errors import ValidationError

INSPECTION_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_INSPECTION_TIMES = ("08:55", "11:30", "21:30")
INSPECTION_TIMES = ((8, 55), (11, 30), (21, 30))
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def normalize_inspection_times(value: object) -> tuple[str, ...]:
    """Validate and sort a non-empty list of unique ``HH:mm`` values."""
    if isinstance(value, str):
        values: Iterable[object] = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ValidationError("每日巡检时间必须是 HH:mm 时间点列表")

    normalized: list[str] = []
    for raw_time in values:
        if not isinstance(raw_time, str):
            raise ValidationError("每日巡检时间必须使用 HH:mm 格式")
        item = raw_time.strip()
        if not _TIME_PATTERN.fullmatch(item):
            raise ValidationError("每日巡检时间必须使用 24 小时制 HH:mm 格式")
        normalized.append(item)
    if not normalized:
        raise ValidationError("至少需要保留一个每日巡检时间点")
    if len(set(normalized)) != len(normalized):
        raise ValidationError("每日巡检时间点不能重复")
    return tuple(sorted(normalized))


def scheduled_slot(
    value: datetime, times: Iterable[str] = DEFAULT_INSPECTION_TIMES
) -> str | None:
    """Return the local date and configured time when ``value`` is in its minute."""
    current = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    local = current.astimezone(INSPECTION_TIMEZONE)
    local_time = f"{local.hour:02d}:{local.minute:02d}"
    if local_time not in times:
        return None
    return f"{local.date().isoformat()}:{local_time.replace(':', '')}"


def next_inspection_at(
    now: datetime, times: Iterable[str] = DEFAULT_INSPECTION_TIMES
) -> datetime:
    """Return the next configured Shanghai slot, skipping slots already missed."""
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    local_now = current.astimezone(INSPECTION_TIMEZONE)
    parsed_times = tuple(
        (int(item[:2]), int(item[3:])) for item in normalize_inspection_times(tuple(times))
    )
    for day_offset in range(2):
        scheduled_day = local_now.date() + timedelta(days=day_offset)
        for hour, minute in parsed_times:
            candidate = datetime.combine(
                scheduled_day,
                time(hour=hour, minute=minute),
                tzinfo=INSPECTION_TIMEZONE,
            )
            if candidate >= local_now:
                return candidate
    raise RuntimeError("无法计算下一次问题巡检时间")
