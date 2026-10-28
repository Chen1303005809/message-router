#!/usr/bin/env python3
"""Clear issue workflow data and all organization teams before launch.

Dry-run is the default. Execution requires an explicit confirmation token and
the issue/team/user counts observed during the dry-run. Users and web sessions
are preserved. Media objects under the application's ``media/`` prefix are
removed after the database transaction commits.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from sqlalchemy import Engine, delete, func, inspect, select, text, update
from sqlalchemy.engine import make_url

from kefu.config import Settings
from kefu.media.storage import object_storage_from_settings
from kefu.persistence.database import create_engine_from_url
from kefu.persistence.models import Base

CONFIRMATION = "DELETE-KEFU-LAUNCH-DATA"

# Child tables come first. Some of the legacy foreign keys do not specify
# ON DELETE CASCADE, so keep this order explicit rather than relying on the DB.
DELETE_ORDER = (
    "group_issue_intake_turns",
    "issue_status_update_jobs",
    "delivery_items",
    "deliveries",
    "deferred_passive_replies",
    "inbound_messages",
    "issue_entry_parts",
    "issue_inspection_states",
    "issue_intake_drafts",
    "group_issue_intake_sessions",
    "issue_draft_processing_tasks",
    "message_outbox",
    "bug_daily_sequences",
    "issue_entries",
    "issues",
    "stored_media",
    "wecom_channels",
    "team_memberships",
    "teams",
)

PRESERVED_TABLES = ("users", "web_sessions")
REQUIRED_TABLES = set(DELETE_ORDER) | set(PRESERVED_TABLES) | {"alembic_version"}


def _count_rows(connection, table_name: str) -> int:
    table = Base.metadata.tables[table_name]
    return int(connection.scalar(select(func.count()).select_from(table)) or 0)


def _database_target(database_url: str) -> str:
    return make_url(database_url).render_as_string(hide_password=True)


def _version_string(connection) -> str:
    versions = connection.scalars(text("SELECT version_num FROM alembic_version")).all()
    return ", ".join(str(version) for version in versions) or "(empty)"


def _check_schema(engine: Engine) -> None:
    existing = set(inspect(engine).get_table_names())
    missing = sorted(REQUIRED_TABLES - existing)
    if missing:
        missing_text = ", ".join(missing)
        raise RuntimeError(
            "数据库缺少当前版本所需的数据表："
            f"{missing_text}。请先停止应用并运行 alembic upgrade head，再重试。"
        )


def _show_plan(engine: Engine, database_url: str) -> dict[str, int]:
    _check_schema(engine)
    with engine.connect() as connection:
        version = _version_string(connection)
        counts = {name: _count_rows(connection, name) for name in DELETE_ORDER}
        preserved = {name: _count_rows(connection, name) for name in PRESERVED_TABLES}
        team_table = Base.metadata.tables["teams"]
        teams = connection.execute(
            select(team_table.c.kind, team_table.c.name).order_by(
                team_table.c.kind,
                team_table.c.name,
            )
        ).all()

    print(f"数据库：{_database_target(database_url)}")
    print(f"Alembic 版本：{version}")
    print("将清空的问题与队列数据：")
    for name in DELETE_ORDER:
        print(f"  {name}: {counts[name]}")
    print("将删除的团队：")
    if teams:
        for kind, name in teams:
            print(f"  {kind}: {name}")
    else:
        print("  (none)")
    print("保留：")
    for name, count in preserved.items():
        print(f"  {name}: {count}")
    print("媒体对象：执行时会删除对象存储的 media/ 前缀；预览不枚举对象。")
    counts["users"] = preserved["users"]
    return counts


def _execute(
    engine: Engine,
    settings: Settings,
    expected_issues: int,
    expected_teams: int,
    expected_users: int,
) -> None:
    _check_schema(engine)
    deleted: dict[str, int] = {}

    with engine.begin() as connection:
        issue_count = _count_rows(connection, "issues")
        team_count = _count_rows(connection, "teams")
        user_count = _count_rows(connection, "users")
        if issue_count != expected_issues:
            raise RuntimeError(
                f"issues 数量已变化：预期 {expected_issues}，现在 {issue_count}。"
                "请重新执行预览并核对。"
            )
        if team_count != expected_teams:
            raise RuntimeError(
                f"teams 数量已变化：预期 {expected_teams}，现在 {team_count}。"
                "请重新执行预览并核对。"
            )
        if user_count != expected_users:
            raise RuntimeError(
                f"users 数量已变化：预期 {expected_users}，现在 {user_count}。"
                "请重新执行预览并核对。"
            )

        preserved_before = {
            name: _count_rows(connection, name) for name in PRESERVED_TABLES
        }

        # Remove self-references before clearing the rows they point to.
        connection.execute(
            update(Base.metadata.tables["issue_entries"]).values(corrects_entry_id=None)
        )
        connection.execute(update(Base.metadata.tables["issues"]).values(related_case_id=None))

        for name in DELETE_ORDER:
            result = connection.execute(delete(Base.metadata.tables[name]))
            deleted[name] = max(result.rowcount or 0, 0)

        preserved_after = {
            name: _count_rows(connection, name) for name in PRESERVED_TABLES
        }
        if preserved_after != preserved_before:
            raise RuntimeError(
                "清理期间 users 或 web_sessions 数量发生变化；已回滚数据库清理。"
            )

    # Object storage cannot participate in the SQL transaction. Delete media
    # only after the database commit; if storage deletion fails, rerunning this
    # command is safe and retries the media-prefix deletion.
    try:
        object_storage_from_settings(settings).delete_prefix("media/")
    except Exception as error:
        raise RuntimeError(
            "数据库清理已提交，但 media/ 对象删除失败。请保持服务停止，"
            "重新预览后用 --expect-issues 0 --expect-teams 0 --expect-users "
            f"{preserved_before['users']} 重跑以重试对象删除。原始错误：{error}"
        ) from error

    with engine.connect() as connection:
        remaining = {name: _count_rows(connection, name) for name in DELETE_ORDER}
        preserved = {name: _count_rows(connection, name) for name in PRESERVED_TABLES}
    not_empty = {name: count for name, count in remaining.items() if count}
    if not_empty:
        raise RuntimeError(f"清理后仍有目标数据：{not_empty}")

    print("清理完成。")
    for name in DELETE_ORDER:
        print(f"  已删除 {name}: {deleted[name]}")
    for name, count in preserved.items():
        print(f"  保留 {name}: {count}")
    print("  已删除媒体对象前缀：media/")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="上线前清理所有问题数据、异步队列和咨询/研发团队；默认只预览。"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只显示待清理数量（默认）")
    mode.add_argument("--execute", action="store_true", help="执行清理")
    parser.add_argument("--confirm", help=f"执行确认口令：{CONFIRMATION}")
    parser.add_argument("--expect-issues", type=int, help="预览时看到的 issues 数量")
    parser.add_argument("--expect-teams", type=int, help="预览时看到的 teams 数量")
    parser.add_argument("--expect-users", type=int, help="预览时看到的 users 数量")
    args = parser.parse_args(argv)

    if args.execute:
        if args.confirm != CONFIRMATION:
            parser.error(f"执行清理必须附带 --confirm {CONFIRMATION}")
        if (
            args.expect_issues is None
            or args.expect_teams is None
            or args.expect_users is None
        ):
            parser.error(
                "执行清理必须附带预览得到的 --expect-issues、--expect-teams 和 --expect-users 数量"
            )
        if args.expect_issues < 0 or args.expect_teams < 0 or args.expect_users < 0:
            parser.error("预期数量不能小于 0")
    elif (
        args.confirm
        or args.expect_issues is not None
        or args.expect_teams is not None
        or args.expect_users is not None
    ):
        parser.error(
            "--confirm、--expect-issues、--expect-teams 和 --expect-users 只能与 --execute 一起使用"
        )
    return args


def main(argv: Sequence[str] | None = None) -> int:
    if sys.version_info < (3, 12):
        print("需要 Python 3.12 或更新版本。", file=sys.stderr)
        return 2

    args = _parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "未设置 DATABASE_URL。为避免误用本地默认数据库，请显式设置目标数据库连接。",
            file=sys.stderr,
        )
        return 2

    settings = Settings.from_env()
    engine = create_engine_from_url(settings.database_url)
    try:
        counts = _show_plan(engine, settings.database_url)
        if not args.execute:
            print("当前为预览模式，没有修改数据库或对象存储。")
            print(
                "正式执行时请使用预览中的数量："
                f"--expect-issues {counts['issues']} --expect-teams {counts['teams']} "
                f"--expect-users {counts['users']}"
            )
            return 0

        _execute(
            engine,
            settings,
            args.expect_issues,
            args.expect_teams,
            args.expect_users,
        )
        return 0
    except Exception as error:
        print(f"清理失败：{error}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
