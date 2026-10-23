"""Fixed daily schedule for open-issue summaries."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

INSPECTION_TIMEZONE = ZoneInfo("Asia/Shanghai")
INSPECTION_TIMES = ((8, 55), (11, 30), (21, 30))


def scheduled_slot(value: datetime) -> str | None:
    """Return the local date and slot key when ``value`` is in a scheduled minute."""
    current = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    local = current.astimezone(INSPECTION_TIMEZONE)
    if (local.hour, local.minute) not in INSPECTION_TIMES:
        return None
    return f"{local.date().isoformat()}:{local.hour:02d}{local.minute:02d}"


def next_inspection_at(now: datetime) -> datetime:
    """Return the next fixed Shanghai slot, skipping slots already missed."""
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    local_now = current.astimezone(INSPECTION_TIMEZONE)
    for day_offset in range(2):
        scheduled_day = local_now.date() + timedelta(days=day_offset)
        for hour, minute in INSPECTION_TIMES:
            candidate = datetime.combine(
                scheduled_day,
                time(hour=hour, minute=minute),
                tzinfo=INSPECTION_TIMEZONE,
            )
            if candidate >= local_now:
                return candidate
    raise RuntimeError("无法计算下一次问题巡检时间")
