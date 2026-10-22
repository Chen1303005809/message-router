"""Issue intake and lifecycle interface over the existing timeline workflow."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import (
    Actor,
    Command,
    CommandResult,
    CreateCase,
    GroupActor,
    ImagePart,
    MediaAccess,
    TextPart,
)
from kefu.case_desk.errors import Conflict, NotFound, ValidationError
from kefu.case_desk.markers import new_case_ref
from kefu.case_desk.service import CaseDesk, utc_now
from kefu.config import Settings
from kefu.issues.contracts import (
    ConfirmedIssue,
    IssueCommandResult,
    IssueFields,
    IssueFilter,
    IssueIntake,
    IssueIntakeDraftView,
    IssueOverview,
    IssuePage,
    IssueView,
)
from kefu.persistence.models import (
    Case,
    CaseEntryPart,
    CasePriority,
    IssueCurrentStatus,
    IssueIntakeDraft,
    IssueModule,
    PartKind,
    StoredMedia,
    TeamKind,
)
from kefu.routing.directory import DatabaseRoutingDirectory

_IMAGE_PLACEHOLDER = re.compile(r"\[图片\]")
logger = logging.getLogger(__name__)


def map_image_placeholders(
    text: str, media_ids: Sequence[UUID]
) -> tuple[TextPart | ImagePart, ...]:
    """Map uploaded images to ``[图片]`` occurrences while preserving every text fragment."""
    if not isinstance(text, str):
        raise ValidationError("粘贴内容必须是文本")
    parts: list[TextPart | ImagePart] = []
    image_index = 0
    cursor = 0
    for match in _IMAGE_PLACEHOLDER.finditer(text):
        if match.start() > cursor:
            parts.append(TextPart(text[cursor : match.start()]))
        if image_index < len(media_ids):
            parts.append(ImagePart(media_ids[image_index]))
            image_index += 1
        else:
            parts.append(TextPart(match.group(0)))
        cursor = match.end()
    if cursor < len(text):
        parts.append(TextPart(text[cursor:]))
    if image_index < len(media_ids):
        if parts and isinstance(parts[-1], TextPart):
            parts[-1] = TextPart(parts[-1].text + "\n")
        parts.extend(ImagePart(media_id) for media_id in media_ids[image_index:])
    if not any(isinstance(part, TextPart) and part.text.strip() for part in parts):
        raise ValidationError("问题内容至少需要包含一段文字")
    return tuple(parts)


class IssueManagement:
    """Small program interface for issue intake, confirmation, and existing timelines."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        desk: CaseDesk,
        settings: Settings,
        *,
        storage=None,
    ) -> None:
        self._session_factory = session_factory
        self._desk = desk
        self._settings = settings
        self._storage = storage
        self._directory = DatabaseRoutingDirectory()

    def create_intake_draft(self, intake: IssueIntake, actor: Actor) -> IssueIntakeDraftView:
        self.cleanup_expired_drafts()
        if not intake.raw_text.strip():
            raise ValidationError("请粘贴咨询消息内容")
        self._validate_fields(intake.fields)
        now = utc_now()
        expires_at = now + timedelta(hours=2)
        initial_expires_at = intake.fields.deadline or now + timedelta(
            hours=self._settings.issue_default_expiry_hours
        )
        if initial_expires_at.tzinfo is None or initial_expires_at.utcoffset() is None:
            raise ValidationError("问题初次过期时间必须包含时区")
        if initial_expires_at <= now:
            raise ValidationError("问题初次过期时间必须晚于当前时间")
        normalized_number: str | None = None
        with self._session_factory() as session, session.begin():
            self._directory.assert_consultant_in_queue(
                session,
                consultant_id=actor.user_id,
                queue_id=intake.consult_queue_id,
            )
            self._directory.active_consult_channel(session, intake.consult_queue_id)
            if intake.fields.initial_acceptance_team_id is not None:
                self._directory.get_team(
                    session,
                    intake.fields.initial_acceptance_team_id,
                    kind=TeamKind.DEV,
                )
                self._directory.active_channel(session, intake.fields.initial_acceptance_team_id)
            for _ in range(32):
                candidate = new_case_ref()
                if (
                    session.scalar(select(Case.id).where(Case.case_ref == candidate)) is None
                    and session.scalar(
                        select(IssueIntakeDraft.id).where(
                            IssueIntakeDraft.issue_number == candidate
                        )
                    )
                    is None
                ):
                    normalized_number = candidate
                    break
            if normalized_number is None:
                raise Conflict("无法生成未冲突的问题编号")
            fields = intake.fields
            actor_user = self._directory.get_user(session, actor.user_id)
            fields_json = _fields_to_json(
                fields,
                consult_queue_id=intake.consult_queue_id,
                first_recorder_id=actor.user_id,
                initial_expires_at=initial_expires_at,
            )
            record = IssueIntakeDraft(
                id=uuid4(),
                issue_number=normalized_number,
                actor_id=actor.user_id,
                raw_text=intake.raw_text,
                fields_json=fields_json,
                parts_json=[_part_to_json(part) for part in intake.parts],
                expires_at=expires_at,
            )
            session.add(record)
            session.flush()
            return IssueIntakeDraftView(
                id=record.id,
                issue_number=record.issue_number,
                actor_id=record.actor_id,
                raw_text=record.raw_text,
                parts=tuple(intake.parts),
                consult_queue_id=intake.consult_queue_id,
                fields=fields,
                first_recorder_name=actor_user.display_name,
                initial_expires_at=initial_expires_at,
                expires_at=expires_at,
                confirmed_issue_id=None,
            )

    def get_intake_draft(self, draft_id: UUID, actor: Actor) -> IssueIntakeDraftView:
        with self._session_factory() as session:
            record = self._owned_draft(session, draft_id, actor)
            actor_user = self._directory.get_user(session, actor.user_id)
            return _draft_view(record, actor_user.display_name)

    def get(self, issue_number: str, actor: Actor) -> IssueView:
        return self._desk.get_case(issue_number, actor)

    def list(self, issue_filter: IssueFilter, actor: Actor) -> IssuePage:
        return self._desk.list_cases(issue_filter, actor)

    def execute(self, command: Command, actor: Actor | GroupActor) -> IssueCommandResult:
        if isinstance(command, CreateCase):
            command = replace(
                command,
                deadline=command.deadline
                or utc_now() + timedelta(hours=self._settings.issue_default_expiry_hours),
                approaching_window_minutes=(
                    command.approaching_window_minutes
                    if command.approaching_window_minutes is not None
                    else self._settings.issue_approaching_window_hours * 60
                ),
            )
        return _issue_command_result(self._desk.execute(command, actor))

    def overview(self, actor: Actor) -> IssueOverview:
        return self._desk.overview(actor)

    def list_consult_queues(self, actor: Actor):
        return self._desk.list_consult_queues(actor)

    def list_dev_teams(self, actor: Actor):
        return self._desk.list_dev_teams(actor)

    def get_media_access(self, issue_number: str, media_id: UUID, actor: Actor) -> MediaAccess:
        return self._desk.get_media_access(issue_number, media_id, actor)

    def confirm_intake(self, draft_id: UUID, fields: IssueFields, actor: Actor) -> ConfirmedIssue:
        self._validate_fields(fields, require_team=True, require_module=True)
        with self._session_factory() as session:
            record = self._owned_draft(session, draft_id, actor)
            if record.confirmed_issue_id is not None:
                issue = session.get(Case, record.confirmed_issue_id)
                if issue is not None:
                    result = self._desk.execute(
                        CreateCase(
                            title=issue.title,
                            consult_queue_id=issue.consult_queue_id,
                            dev_team_id=issue.initial_acceptance_team_id
                            or issue.current_dev_team_id,
                            consult_handler_name=issue.consult_handler_name,
                            issue_number=issue.case_ref,
                            source_msgid=f"issue-intake:{draft_id}",
                        ),
                        actor,
                    )
                    return ConfirmedIssue(
                        issue_number=issue.case_ref,
                        result=_issue_command_result(result),
                    )
            issue_number = record.issue_number
            consult_queue_id = UUID(record.fields_json["consult_queue_id"])
            initial_expires_at = fields.deadline or datetime.fromisoformat(
                record.fields_json["initial_expires_at"]
            )
            parts = tuple(_part_from_json(item) for item in record.parts_json)
            raw_text = record.raw_text
        result = self._desk.execute(
            CreateCase(
                title=(
                    fields.issue_title.strip()
                    or _issue_title(fields.issue_description, raw_text)
                ),
                consult_queue_id=consult_queue_id,
                dev_team_id=fields.initial_acceptance_team_id,
                initial_acceptance_team_id=fields.initial_acceptance_team_id,
                consult_handler_name=self._first_recorder_name(actor),
                priority=CasePriority.NORMAL,
                deadline=initial_expires_at,
                approaching_window_minutes=self._settings.issue_approaching_window_hours * 60,
                issue_number=issue_number,
                issue_description=fields.issue_description.strip(),
                suspected_type=fields.suspected_type.strip() or None,
                issue_current_situation=fields.current_situation.strip() or None,
                issue_current_status=fields.issue_current_status.strip() or "问题未回复",
                issue_source=fields.issue_source.strip() or "工作台",
                customer_name=fields.customer_name.strip() or None,
                customer_contact_name=fields.customer_contact_name.strip() or None,
                customer_contact_method=fields.customer_contact_method.strip() or None,
                parts=parts,
                source_msgid=f"issue-intake:{draft_id}",
            ),
            actor,
        )
        if result.case_ref is None:
            raise Conflict("问题确认没有返回问题编号")
        public_result = _issue_command_result(result)
        with self._session_factory() as session, session.begin():
            record = self._owned_draft(session, draft_id, actor, allow_expired=True)
            issue = session.scalar(select(Case).where(Case.case_ref == public_result.issue_number))
            if issue is None:
                raise NotFound("已确认的问题不存在")
            record.confirmed_issue_id = issue.id
            record.fields_json = _fields_to_json(
                fields,
                consult_queue_id=consult_queue_id,
                first_recorder_id=actor.user_id,
                initial_expires_at=initial_expires_at,
            )
        return ConfirmedIssue(issue_number=public_result.issue_number, result=public_result)

    def cleanup_expired_drafts(self, now: datetime | None = None) -> int:
        current = now or utc_now()
        with self._session_factory() as session, session.begin():
            drafts = session.scalars(
                select(IssueIntakeDraft).where(
                    IssueIntakeDraft.expires_at <= current,
                    IssueIntakeDraft.confirmed_issue_id.is_(None),
                )
            ).all()
            media_ids = {
                UUID(part["media_id"])
                for draft in drafts
                for part in draft.parts_json
                if part.get("kind") == PartKind.IMAGE.value
            }
            media = (
                session.scalars(
                    select(StoredMedia).where(
                        StoredMedia.id.in_(media_ids),
                        StoredMedia.id.not_in(
                            select(CaseEntryPart.media_id).where(
                                CaseEntryPart.media_id.is_not(None)
                            )
                        ),
                    )
                ).all()
                if media_ids
                else []
            )
            object_keys = [item.object_key for item in media]
            for draft in drafts:
                session.delete(draft)
            for item in media:
                session.delete(item)
        if self._storage is not None:
            for object_key in object_keys:
                try:
                    self._storage.delete_prefix(object_key)
                except Exception:
                    logger.warning("expired_intake_object_cleanup_failed object_key=%s", object_key)
        return len(drafts)

    def _owned_draft(
        self,
        session: Session,
        draft_id: UUID,
        actor: Actor,
        *,
        allow_expired: bool = False,
    ) -> IssueIntakeDraft:
        record = session.get(IssueIntakeDraft, draft_id)
        if record is None or record.actor_id != actor.user_id:
            raise NotFound("问题草稿不存在")
        if (
            not allow_expired
            and _as_utc(record.expires_at) <= utc_now()
            and record.confirmed_issue_id is None
        ):
            raise NotFound("问题草稿已过期，请重新上传")
        return record

    def _validate_fields(
        self,
        fields: IssueFields,
        *,
        require_team: bool = False,
        require_module: bool = False,
    ) -> None:
        if not fields.issue_description.strip():
            raise ValidationError("问题描述不能为空")
        if len(fields.issue_title) > 512:
            raise ValidationError("问题标题不能超过512个字符")
        if len(fields.issue_description) > 20_000:
            raise ValidationError("问题描述不能超过20000个字符")
        if fields.issue_current_status not in {status.value for status in IssueCurrentStatus}:
            raise ValidationError("请选择有效的问题状态")
        suspected_type = fields.suspected_type.strip()
        if suspected_type and suspected_type not in {module.value for module in IssueModule}:
            raise ValidationError("请选择有效的问题模块")
        if require_module and not suspected_type:
            raise ValidationError("请选择问题模块")
        if len(fields.current_situation) > 20_000:
            raise ValidationError("现状说明不能超过20000个字符")
        if len(fields.issue_source) > 256:
            raise ValidationError("问题来源不能超过256个字符")
        if len(fields.customer_name) > 256:
            raise ValidationError("客户 / 企业名称不能超过256个字符")
        if len(fields.customer_contact_name) > 256:
            raise ValidationError("联系人不能超过256个字符")
        if len(fields.customer_contact_method) > 256:
            raise ValidationError("联系方式不能超过256个字符")
        if require_team and fields.initial_acceptance_team_id is None:
            raise ValidationError("请选择初次受理组")

    def _first_recorder_name(self, actor: Actor) -> str:
        with self._session_factory() as session:
            return self._directory.get_user(session, actor.user_id).display_name


def _issue_title(description: str, raw_text: str) -> str:
    source = description.strip() or raw_text.strip()
    title = next((line.strip() for line in source.splitlines() if line.strip()), "问题").strip()
    return title[:512]


def _issue_command_result(result: CommandResult) -> IssueCommandResult:
    return IssueCommandResult(
        issue_number=result.case_ref,
        version=result.case_version,
        entry_id=result.entry_id,
        delivery_ids=result.delivery_ids,
        idempotent=result.idempotent,
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _fields_to_json(
    fields: IssueFields,
    *,
    consult_queue_id: UUID,
    first_recorder_id: UUID,
    initial_expires_at: datetime,
) -> dict[str, str]:
    return {
        "issue_title": fields.issue_title,
        "issue_description": fields.issue_description,
        "initial_acceptance_team_id": (
            str(fields.initial_acceptance_team_id)
            if fields.initial_acceptance_team_id is not None
            else ""
        ),
        "suspected_type": fields.suspected_type,
        "current_situation": fields.current_situation,
        "issue_current_status": fields.issue_current_status,
        "issue_source": fields.issue_source,
        "deadline": fields.deadline.isoformat() if fields.deadline else "",
        "customer_name": fields.customer_name,
        "customer_contact_name": fields.customer_contact_name,
        "customer_contact_method": fields.customer_contact_method,
        "consult_queue_id": str(consult_queue_id),
        "first_recorder_id": str(first_recorder_id),
        "initial_expires_at": initial_expires_at.isoformat(),
    }


def _fields_from_json(data: dict[str, str]) -> IssueFields:
    deadline = data.get("deadline")
    return IssueFields(
        issue_title=(
            data["issue_title"]
            if "issue_title" in data
            else _issue_title(data.get("issue_description", ""), "")
        ),
        issue_description=data.get("issue_description", ""),
        initial_acceptance_team_id=(
            UUID(data["initial_acceptance_team_id"])
            if data.get("initial_acceptance_team_id")
            else None
        ),
        suspected_type=data.get("suspected_type", ""),
        current_situation=data.get("current_situation", ""),
        issue_current_status=data.get("issue_current_status", "问题未回复"),
        issue_source=data.get("issue_source", "工作台"),
        deadline=datetime.fromisoformat(deadline) if deadline else None,
        customer_name=data.get("customer_name", ""),
        customer_contact_name=data.get("customer_contact_name", ""),
        customer_contact_method=data.get("customer_contact_method", ""),
    )


def _part_to_json(part: TextPart | ImagePart) -> dict[str, str | int]:
    if isinstance(part, TextPart):
        return {"kind": PartKind.TEXT.value, "text": part.text}
    return {"kind": PartKind.IMAGE.value, "media_id": str(part.media_id)}


def _part_from_json(data: dict[str, str | int]) -> TextPart | ImagePart:
    if data.get("kind") == PartKind.TEXT.value:
        return TextPart(str(data.get("text", "")))
    if data.get("kind") == PartKind.IMAGE.value:
        return ImagePart(UUID(str(data["media_id"])))
    raise ValidationError("草稿包含无法识别的内容片段")


def _draft_view(record: IssueIntakeDraft, first_recorder_name: str) -> IssueIntakeDraftView:
    fields = _fields_from_json(record.fields_json)
    return IssueIntakeDraftView(
        id=record.id,
        issue_number=record.issue_number,
        actor_id=record.actor_id,
        raw_text=record.raw_text,
        parts=tuple(_part_from_json(item) for item in record.parts_json),
        consult_queue_id=UUID(record.fields_json["consult_queue_id"]),
        fields=fields,
        first_recorder_name=first_recorder_name,
        initial_expires_at=datetime.fromisoformat(record.fields_json["initial_expires_at"]),
        expires_at=record.expires_at,
        confirmed_issue_id=record.confirmed_issue_id,
    )
