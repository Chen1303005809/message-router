from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from kefu.case_desk.service import CaseDesk
from kefu.persistence.models import (
    Base,
    MembershipRole,
    StoredMedia,
    Team,
    TeamKind,
    TeamMembership,
    User,
    WeComChannel,
)


@dataclass(frozen=True, slots=True)
class DeskContext:
    desk: CaseDesk
    session_factory: sessionmaker[Session]
    users: dict[str, UUID]
    teams: dict[str, UUID]
    media_id: UUID


@pytest.fixture
def desk_context() -> DeskContext:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    users = {
        name: uuid4()
        for name in (
            "consult_a",
            "consult_b",
            "consult_admin",
            "dev_a",
            "dev_b",
            "dev_c",
            "dev_admin",
            "outsider",
        )
    }
    teams = {name: uuid4() for name in ("consult", "dev_a", "dev_b")}
    media_id = uuid4()
    now = datetime.now(UTC)

    with factory() as session, session.begin():
        session.add_all(
            [
                User(id=users["consult_a"], wecom_userid="consult-a", display_name="咨询甲"),
                User(id=users["consult_b"], wecom_userid="consult-b", display_name="咨询乙"),
                User(
                    id=users["consult_admin"],
                    wecom_userid="consult-admin",
                    display_name="咨询管理员",
                ),
                User(id=users["dev_a"], wecom_userid="dev-a", display_name="研发甲"),
                User(id=users["dev_b"], wecom_userid="dev-b", display_name="研发乙"),
                User(id=users["dev_c"], wecom_userid="dev-c", display_name="研发丙"),
                User(
                    id=users["dev_admin"],
                    wecom_userid="dev-admin",
                    display_name="研发管理员",
                ),
                User(id=users["outsider"], wecom_userid="outsider", display_name="外部人员"),
            ]
        )
        session.add_all(
            [
                Team(id=teams["consult"], kind=TeamKind.CONSULT_QUEUE, name="咨询队列"),
                Team(id=teams["dev_a"], kind=TeamKind.DEV, name="研发一组"),
                Team(id=teams["dev_b"], kind=TeamKind.DEV, name="研发二组"),
            ]
        )
        memberships = [
            ("consult", "consult_a", MembershipRole.MEMBER),
            ("consult", "consult_b", MembershipRole.MEMBER),
            ("consult", "consult_admin", MembershipRole.ADMIN),
            ("dev_a", "dev_a", MembershipRole.MEMBER),
            ("dev_a", "dev_b", MembershipRole.MEMBER),
            ("dev_a", "dev_admin", MembershipRole.ADMIN),
            ("dev_b", "dev_c", MembershipRole.MEMBER),
        ]
        session.add_all(
            [
                TeamMembership(
                    team_id=teams[team_name],
                    user_id=users[user_name],
                    role=role,
                    valid_from=now,
                )
                for team_name, user_name, role in memberships
            ]
        )
        session.add_all(
            [
                WeComChannel(
                    id=uuid4(),
                    team_id=teams["dev_a"],
                    chatid="chat-dev-a",
                    initialized_at=now,
                ),
                WeComChannel(
                    id=uuid4(),
                    team_id=teams["dev_b"],
                    chatid="chat-dev-b",
                    initialized_at=now,
                ),
            ]
        )
        session.add(
            StoredMedia(
                id=media_id,
                object_key="test/image.png",
                mime_type="image/png",
                byte_size=42,
                sha256="0" * 64,
            )
        )

    try:
        yield DeskContext(
            desk=CaseDesk(factory),
            session_factory=factory,
            users=users,
            teams=teams,
            media_id=media_id,
        )
    finally:
        engine.dispose()
