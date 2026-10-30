"""Issue identifiers that need a database transaction for allocation."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kefu.persistence.models import BugDailySequence

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def next_bug_number(session: Session, created_at: datetime) -> str:
    """Allocate MMDDNN from a per-Shanghai-day counter, expanding past 99."""
    local_created_at = created_at.astimezone(_SHANGHAI)
    date_key = local_created_at.date().isoformat()
    dialect = session.get_bind().dialect.name

    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        return _next_bug_number_with_row_lock(session, date_key, local_created_at)

    session.execute(
        insert(BugDailySequence)
        .values(date_key=date_key, last_sequence=0)
        .on_conflict_do_nothing(index_elements=[BugDailySequence.date_key])
    )
    sequence = session.scalar(
        update(BugDailySequence)
        .where(BugDailySequence.date_key == date_key)
        .values(last_sequence=BugDailySequence.last_sequence + 1)
        .returning(BugDailySequence.last_sequence)
    )
    if sequence is None:
        raise RuntimeError("无法分配今日 BUG 编号")
    return f"{local_created_at:%m%d}{sequence:02d}"


def _next_bug_number_with_row_lock(
    session: Session, date_key: str, local_created_at: datetime
) -> str:
    sequence_row = session.scalar(
        select(BugDailySequence).where(BugDailySequence.date_key == date_key).with_for_update()
    )
    if sequence_row is None:
        try:
            with session.begin_nested():
                sequence_row = BugDailySequence(date_key=date_key, last_sequence=1)
                session.add(sequence_row)
                session.flush()
            sequence = 1
        except IntegrityError as error:
            # A concurrent allocator created this date row after our first
            # read. The savepoint keeps the surrounding issue transaction
            # usable so we can lock and increment the winner's row.
            sequence_row = session.scalar(
                select(BugDailySequence)
                .where(BugDailySequence.date_key == date_key)
                .with_for_update()
            )
            if sequence_row is None:
                raise RuntimeError("无法创建 BUG 编号日计数器") from error
            sequence_row.last_sequence += 1
            sequence = sequence_row.last_sequence
    else:
        sequence_row.last_sequence += 1
        sequence = sequence_row.last_sequence
    return f"{local_created_at:%m%d}{sequence:02d}"
