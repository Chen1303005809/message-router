"""CaseDeskCommands keeps CaseDesk behavior grouped by responsibility."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from kefu.case_desk.contracts import (
    SYSTEM_ACTOR,
    Actor,
    AdjustCaseDeadline,
    CloseCase,
    Command,
    CommandResult,
    ContentPart,
    CorrectEntry,
    CreateCase,
    DeliveryItemFailed,
    DeliveryItemSucceeded,
    ExtendCaseDeadline,
    GroupActor,
    ImagePart,
    PostFormalMessage,
    ReopenCase,
    SetCaseApproachingWindow,
    SetCaseDeadline,
    SetCaseStatus,
    SetIssueCurrentStatus,
    SystemActor,
    TextPart,
    TransferConsultant,
    TransferDevTeam,
    UpdateCaseMetadata,
)
from kefu.case_desk.errors import Conflict, Forbidden, NotFound, ValidationError
from kefu.case_desk.markers import new_case_ref, normalize_case_ref
from kefu.case_desk.numbering import next_bug_number
from kefu.persistence.models import (
    DEFAULT_APPROACHING_WINDOW_MINUTES,
    DEFAULT_CASE_DEADLINE_HOURS,
    MIN_DEADLINE_INCREMENT_MINUTES,
    Case,
    CaseEntry,
    CaseEntryKind,
    CaseEntryPart,
    CaseStatus,
    DeliveryDestination,
    DeliveryItemStatus,
    DeliveryStatus,
    EntrySide,
    IssueCurrentStatus,
    IssueIntakeDraft,
    IssueModule,
    IssueStatusUpdateJob,
    IssueStatusUpdateState,
    LifecycleStatus,
    MessageIntent,
    PartKind,
    StoredMedia,
    Team,
    TeamKind,
    User,
    WaitingOn,
)
from kefu.routing.directory import TeamChannel

from .service_support import (
    DEFERRED_PASSIVE_PENDING_MODE,
    _as_utc,
    _case_customer_group_name,
    _clean_optional_text,
    _validated_deadline_adjustment_minutes,
    _validated_deadline_minutes,
    utc_now,
)


class CaseDeskCommands:
    """CaseDesk Commands operations."""

    def execute(self, command: Command, actor: Actor | GroupActor | SystemActor) -> CommandResult:
        """Apply a closed command set atomically and return its stable result."""
        with self._session_factory() as session, session.begin():
            if isinstance(command, (DeliveryItemSucceeded, DeliveryItemFailed)):
                if actor is not SYSTEM_ACTOR:
                    raise Forbidden("投递结果只能由系统记录")
                if isinstance(command, DeliveryItemSucceeded):
                    return self._delivery_item_succeeded(session, command)
                return self._delivery_item_failed(session, command)

            if isinstance(command, PostFormalMessage) and isinstance(actor, GroupActor):
                return self._post_formal_message(session, command, actor)
            if isinstance(command, CreateCase) and isinstance(actor, GroupActor):
                return self._create_case(session, command, actor)
            if not isinstance(actor, Actor):
                raise Forbidden("此操作需要已认证的人员身份")
            if isinstance(command, CreateCase):
                return self._create_case(session, command, actor)
            if isinstance(command, (AdjustCaseDeadline, ExtendCaseDeadline)):
                return self._adjust_case_deadline(session, command, actor)
            if isinstance(command, SetCaseDeadline):
                return self._set_case_deadline(session, command, actor)
            if isinstance(command, SetCaseApproachingWindow):
                return self._set_case_approaching_window(session, command, actor)
            if isinstance(command, PostFormalMessage):
                return self._post_formal_message(session, command, actor)
            if isinstance(command, UpdateCaseMetadata):
                return self._update_case_metadata(session, command, actor)
            if isinstance(command, SetIssueCurrentStatus):
                return self._set_issue_current_status(session, command, actor)
            if isinstance(command, TransferConsultant):
                return self._transfer_consultant(session, command, actor)
            if isinstance(command, TransferDevTeam):
                return self._transfer_dev_team(session, command, actor)
            if isinstance(command, CloseCase):
                return self._close_case(session, command, actor)
            if isinstance(command, SetCaseStatus):
                return self._set_case_status(session, command, actor)
            if isinstance(command, ReopenCase):
                return self._reopen_case(session, command, actor)
            if isinstance(command, CorrectEntry):
                return self._correct_entry(session, command, actor)
            raise ValidationError(f"不支持的问题命令：{type(command).__name__}")

    def _create_case(
        self, session: Session, command: CreateCase, actor: Actor | GroupActor
    ) -> CommandResult:
        entry_actor_user: User | None
        entry_actor_name: str | None = None
        if isinstance(actor, Actor):
            actor_user = self._directory.get_user(session, actor.user_id)
            self._directory.assert_consultant_in_queue(
                session, consultant_id=actor.user_id, queue_id=command.consult_queue_id
            )
            actor_id = actor.user_id
            handler_name = command.consult_handler_name or actor_user.display_name
            entry_actor_user = actor_user
        else:
            if actor.side is not EntrySide.CONSULT or not command.origin_chatid:
                raise Forbidden("只有已绑定的咨询群可以创建问题")
            source_team = self._directory.active_team_for_chatid(session, command.origin_chatid)
            if source_team.kind is not TeamKind.CONSULT_QUEUE:
                raise Forbidden("当前群聊不是咨询队列群")
            if source_team.id != command.consult_queue_id:
                raise Forbidden("咨询群与问题所属队列不匹配")
            actor_user = None
            actor_id = None
            handler_name = source_team.name
            entry_actor_user = None
            if actor.user_id is not None:
                possible_author = session.get(User, actor.user_id)
                if (
                    possible_author is not None
                    and possible_author.active
                    and self._directory.is_member(
                        session, user_id=possible_author.id, team_id=source_team.id
                    )
                ):
                    entry_actor_user = possible_author
                    entry_actor_name = possible_author.display_name
        if command.source_msgid:
            existing = self._existing_source_entry(session, command.source_msgid)
            if existing is not None:
                existing_case = self._case_by_id(session, existing.case_id)
                if actor_id is not None:
                    self._directory.assert_case_visible(session, actor_id, existing_case)
                elif existing_case.consult_queue_id != command.consult_queue_id:
                    raise Conflict("该群聊消息已归入另一个问题")
                return self._result(existing_case, existing.id, idempotent=True)

        parts = self._validate_parts(command.parts)
        source_msgid = self._clean_source_msgid(command.source_msgid)
        if source_msgid is not None:
            existing = self._existing_source_entry(session, source_msgid)
            if existing is not None:
                existing_case = self._case_by_id(session, existing.case_id)
                if actor_id is not None:
                    self._directory.assert_case_visible(session, actor_id, existing_case)
                elif existing_case.consult_queue_id != command.consult_queue_id:
                    raise Conflict("该群聊消息已归入另一个问题")
                return self._result(existing_case, existing.id, idempotent=True)
        self._assert_media_exists(session, parts)
        initial_acceptance_team_id = command.initial_acceptance_team_id or command.dev_team_id
        dev_team = self._directory.get_team(session, initial_acceptance_team_id, kind=TeamKind.DEV)
        channel = self._directory.active_channel(session, dev_team.id)
        self._directory.active_consult_channel(session, command.consult_queue_id)
        title = self._validated_title(command.title, parts)
        customer_name = self._validated_customer_name(command.customer_name)
        customer_group_name = self._validated_customer_group_name(command.customer_group_name)
        consult_handler_name = self._validated_handler_name(handler_name)
        related_case_id = self._validated_related_case(session, command.related_case_id)
        created_at = utc_now()
        deadline = created_at + timedelta(hours=DEFAULT_CASE_DEADLINE_HOURS)
        if command.deadline is not None:
            if command.deadline.tzinfo is None or command.deadline.utcoffset() is None:
                raise ValidationError("截止时间必须包含时区")
            deadline = command.deadline.astimezone(UTC)
            if deadline <= created_at:
                raise ValidationError("截止时间必须晚于当前时间")
        issue_number = (
            normalize_case_ref(command.issue_number)
            if command.issue_number
            else self._new_unique_case_ref(session)
        )
        if session.scalar(select(Case.id).where(Case.case_ref == issue_number)) is not None:
            raise Conflict("问题编号已经被使用，请重新生成草稿")
        reservation = session.scalar(
            select(IssueIntakeDraft).where(IssueIntakeDraft.issue_number == issue_number)
        )
        if reservation is not None and source_msgid != f"issue-intake:{reservation.id}":
            raise Conflict("问题编号已预留给另一个上传草稿")
        issue_status = (command.issue_current_status or "问题未回复").strip()
        if issue_status not in {status.value for status in IssueCurrentStatus}:
            raise ValidationError("问题状态无效，请从规定选项中选择")
        issue_description = _clean_optional_text(command.issue_description)
        if issue_description and len(issue_description) > 20_000:
            raise ValidationError("问题描述不能超过20000个字符")
        current_situation = _clean_optional_text(command.issue_current_situation)
        if current_situation and len(current_situation) > 20_000:
            raise ValidationError("现状说明不能超过20000个字符")
        suspected_type = _clean_optional_text(command.suspected_type)
        if suspected_type not in {module.value for module in IssueModule}:
            raise ValidationError("请选择有效的问题模块")
        issue_source = _clean_optional_text(command.issue_source)
        if issue_source and len(issue_source) > 256:
            raise ValidationError("问题来源不能超过256个字符")
        bug_number = next_bug_number(session, created_at)
        approaching_window_minutes = (
            command.approaching_window_minutes
            if command.approaching_window_minutes is not None
            else DEFAULT_APPROACHING_WINDOW_MINUTES
        )
        if approaching_window_minutes < MIN_DEADLINE_INCREMENT_MINUTES:
            raise ValidationError("临期窗口不能小于30分钟")
        case = Case(
            id=uuid4(),
            case_ref=issue_number,
            title=title,
            customer_name=customer_name,
            customer_group_name=customer_group_name,
            customer_contact_name=_clean_optional_text(command.customer_contact_name),
            customer_contact_method=_clean_optional_text(command.customer_contact_method),
            priority=command.priority,
            deadline=deadline,
            approaching_window_minutes=approaching_window_minutes,
            lifecycle_status=LifecycleStatus.OPEN,
            status=CaseStatus.PENDING_CONFIRMATION,
            # A handoff cannot move the wait state before its delivery bundle
            # succeeds, so a newly-created event remains on the submitting side.
            waiting_on=WaitingOn.CONSULT,
            consult_queue_id=command.consult_queue_id,
            current_consultant_id=actor_id,
            consult_handler_name=consult_handler_name,
            current_dev_team_id=channel.team_id,
            initial_acceptance_team_id=dev_team.id,
            creator_id=actor_id,
            issue_description=issue_description,
            suspected_type=suspected_type,
            issue_current_situation=current_situation,
            issue_current_status=issue_status,
            bug_number=bug_number,
            issue_source=issue_source,
            issue_metadata_json=dict(command.issue_metadata),
            initial_expires_at=deadline,
            related_case_id=related_case_id,
            version=1,
            last_entry_sequence=0,
            created_at=created_at,
        )
        session.add(case)
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.FORMAL_MESSAGE,
            side=EntrySide.CONSULT,
            actor_user=entry_actor_user,
            actor_name=entry_actor_name or consult_handler_name,
            intent=MessageIntent.HANDOFF,
            source_msgid=source_msgid,
            metadata={"action": "create_case"},
        )
        self._add_parts(session, entry, parts)
        delivery_id = self._create_formal_delivery(
            session,
            case=case,
            entry=entry,
            destination_type=DeliveryDestination.CHAT,
            destination_address=channel.chatid,
            waiting_on_after_delivery=WaitingOn.DEV,
        )
        return self._result(case, entry.id, delivery_ids=(delivery_id,))

    def _update_case_metadata(
        self, session: Session, command: UpdateCaseMetadata, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_consult_manager(session, actor.user_id, case)
        actor_user = self._directory.get_user(session, actor.user_id)

        new_customer_name = self._validated_customer_name(command.customer_name)
        current_customer_group_name = _case_customer_group_name(case)
        new_customer_group_name = (
            current_customer_group_name
            if command.customer_group_name is None
            else self._validated_customer_group_name(command.customer_group_name)
        )
        new_contact_name = _clean_optional_text(command.customer_contact_name)
        new_contact_method = _clean_optional_text(command.customer_contact_method)
        new_values = {
            "customer_name": new_customer_name,
            "customer_group_name": new_customer_group_name,
            "customer_contact_name": new_contact_name,
            "customer_contact_method": new_contact_method,
            "priority": command.priority.value,
        }
        old_values = {
            "customer_name": case.customer_name,
            "customer_group_name": current_customer_group_name,
            "customer_contact_name": case.customer_contact_name,
            "customer_contact_method": case.customer_contact_method,
            "priority": case.priority.value,
        }
        changes = {
            key: {"from": old_values[key], "to": value}
            for key, value in new_values.items()
            if old_values[key] != value
        }
        if not changes:
            return self._result(case)

        case.customer_name = new_customer_name
        case.customer_group_name = new_customer_group_name
        case.customer_contact_name = new_contact_name
        case.customer_contact_method = new_contact_method
        case.priority = command.priority
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CASE_METADATA_UPDATED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={"changes": changes},
        )
        return self._result(case, entry.id)

    def _set_issue_current_status(
        self, session: Session, command: SetIssueCurrentStatus, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_consult_manager(session, actor.user_id, case)
        if command.issue_current_status not in {status.value for status in IssueCurrentStatus}:
            raise ValidationError("问题状态无效，请从规定选项中选择")
        if case.issue_current_status == command.issue_current_status:
            return self._result(case, None, idempotent=True)

        actor_user = self._directory.get_user(session, actor.user_id)
        previous_status = case.issue_current_status
        case.issue_current_status = command.issue_current_status
        case.version += 1
        pending_jobs = session.scalars(
            select(IssueStatusUpdateJob)
            .where(
                IssueStatusUpdateJob.case_id == case.id,
                IssueStatusUpdateJob.state.in_(
                    (IssueStatusUpdateState.PENDING, IssueStatusUpdateState.PROCESSING)
                ),
            )
            .with_for_update()
        ).all()
        for job in pending_jobs:
            job.state = IssueStatusUpdateState.CANCELLED
            job.lock_token = None
            job.locked_until = None
            job.last_error = None
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CASE_METADATA_UPDATED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "action": "set_issue_current_status",
                "changes": {
                    "issue_current_status": {
                        "from": previous_status,
                        "to": command.issue_current_status,
                    }
                },
            },
        )
        return self._result(case, entry.id)

    def _adjust_case_deadline(
        self,
        session: Session,
        command: AdjustCaseDeadline | ExtendCaseDeadline,
        actor: Actor,
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        if case.lifecycle_status is not LifecycleStatus.OPEN:
            raise ValidationError("已关闭问题不能调整期限")
        self._assert_consult_deadline_access(session, actor.user_id, case)
        if isinstance(command, AdjustCaseDeadline):
            adjustment_minutes = _validated_deadline_adjustment_minutes(command.adjustment_minutes)
            action = "adjust_deadline"
        else:
            adjustment_minutes = _validated_deadline_minutes(command.extension_minutes, "延长期限")
            action = "extend_deadline"
        actor_user = self._directory.get_user(session, actor.user_id)

        old_deadline = _as_utc(case.deadline)
        try:
            new_deadline = old_deadline + timedelta(minutes=adjustment_minutes)
        except OverflowError as error:
            raise ValidationError("截止时间调整超出可设置范围") from error
        case.deadline = new_deadline
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CASE_METADATA_UPDATED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "action": action,
                "changes": {
                    "deadline": {
                        "from": old_deadline.isoformat(),
                        "to": new_deadline.isoformat(),
                    }
                },
            },
        )
        return self._result(case, entry.id)

    def _set_case_deadline(
        self, session: Session, command: SetCaseDeadline, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        if case.lifecycle_status is not LifecycleStatus.OPEN:
            raise ValidationError("已关闭问题不能调整期限")
        self._assert_consult_deadline_access(session, actor.user_id, case)
        if command.deadline.tzinfo is None or command.deadline.utcoffset() is None:
            raise ValidationError("截止时间必须包含时区")

        old_deadline = _as_utc(case.deadline)
        new_deadline = _as_utc(command.deadline)
        if old_deadline == new_deadline:
            return self._result(case)

        actor_user = self._directory.get_user(session, actor.user_id)
        case.deadline = new_deadline
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CASE_METADATA_UPDATED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "action": "set_deadline",
                "changes": {
                    "deadline": {
                        "from": old_deadline.isoformat(),
                        "to": new_deadline.isoformat(),
                    }
                },
            },
        )
        return self._result(case, entry.id)

    def _set_case_approaching_window(
        self, session: Session, command: SetCaseApproachingWindow, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        if case.lifecycle_status is not LifecycleStatus.OPEN:
            raise ValidationError("已关闭问题不能调整期限")
        self._assert_consult_deadline_access(session, actor.user_id, case)
        approaching_minutes = _validated_deadline_minutes(
            command.approaching_window_minutes, "临近期限提醒时间"
        )
        if approaching_minutes == case.approaching_window_minutes:
            return self._result(case)
        actor_user = self._directory.get_user(session, actor.user_id)
        old_minutes = case.approaching_window_minutes
        case.approaching_window_minutes = approaching_minutes
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CASE_METADATA_UPDATED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "action": "set_approaching_window",
                "changes": {
                    "approaching_window_minutes": {
                        "from": old_minutes,
                        "to": approaching_minutes,
                    }
                },
            },
        )
        return self._result(case, entry.id)

    def _post_formal_message(
        self,
        session: Session,
        command: PostFormalMessage,
        actor: Actor | GroupActor,
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        actor_user: User | None
        actor_name: str | None = None
        if isinstance(actor, GroupActor):
            if command.side is not actor.side or not command.origin_chatid:
                raise Forbidden("群聊身份与消息侧别不匹配")
            origin_kind = self._directory.team_kind_for_chatid(session, command.origin_chatid)
            if actor.side is EntrySide.CONSULT:
                expected_kind = TeamKind.CONSULT_QUEUE
            elif actor.side is EntrySide.DEV:
                expected_kind = TeamKind.DEV
            else:
                expected_kind = None
            if expected_kind is None or origin_kind is not expected_kind:
                raise Forbidden("当前群聊未绑定到对应的问题沟通侧")
            actor_user = None
            side = actor.side
            origin_team = self._directory.active_team_for_chatid(session, command.origin_chatid)
            expected_team_id = (
                case.consult_queue_id if side is EntrySide.CONSULT else case.current_dev_team_id
            )
            if origin_team.id != expected_team_id:
                raise Forbidden("当前群聊不属于该问题对应的沟通团队")
            if actor.user_id is not None and origin_team.kind is expected_kind:
                possible_author = session.get(User, actor.user_id)
                if (
                    possible_author is not None
                    and possible_author.active
                    and self._directory.is_member(
                        session, user_id=possible_author.id, team_id=origin_team.id
                    )
                ):
                    actor_user = possible_author
            if side is EntrySide.CONSULT:
                actor_name = actor_user.display_name if actor_user else case.consult_handler_name
            else:
                dev_team = self._directory.get_team(
                    session, case.current_dev_team_id, kind=TeamKind.DEV
                )
                actor_name = (
                    actor_user.display_name
                    if actor_user
                    else dev_team.lead_display_name or dev_team.name
                )
        else:
            actor_user = self._directory.get_user(session, actor.user_id)
            side = cast(
                EntrySide,
                self._directory.side_for_actor(session, actor.user_id, case, command.side),
            )
        existing = self._existing_source_entry(session, command.source_msgid)
        if existing is not None:
            if existing.case_id != case.id:
                raise Conflict("该企业微信消息已归入另一个问题")
            return self._result(case, existing.id, idempotent=True)
        self._assert_expected_version(case, command.expected_version)
        self._assert_open(case)
        parts = self._validate_parts(command.parts)
        self._assert_media_exists(session, parts)
        previous_status = case.status
        mark_processing = (
            isinstance(actor, GroupActor)
            and side is EntrySide.DEV
            and case.lifecycle_status is LifecycleStatus.OPEN
            and case.status
            in {
                CaseStatus.PENDING_CONFIRMATION,
                CaseStatus.WAITING_CUSTOMER,
                CaseStatus.SUSPENDED,
            }
        )
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.FORMAL_MESSAGE,
            side=side,
            actor_user=actor_user,
            actor_name=actor_name,
            intent=command.intent,
            source_msgid=self._clean_source_msgid(command.source_msgid),
            metadata={"action": "post_formal_message"},
        )
        self._add_parts(session, entry, parts)
        session.add(
            IssueStatusUpdateJob(
                id=uuid4(),
                case_id=case.id,
                entry_id=entry.id,
                entry_sequence=entry.sequence,
                next_attempt_at=utc_now(),
            )
        )
        destination_type, destination_address = self._destination_for_side(session, case, side)
        wait_after = (
            self._opposite_waiting_side(side) if command.intent is MessageIntent.HANDOFF else None
        )
        delivery_status = (
            DeliveryStatus.SENT if command.suppress_delivery else DeliveryStatus.PENDING
        )
        item_status = (
            DeliveryItemStatus.SENT if command.suppress_delivery else DeliveryItemStatus.PENDING
        )
        item_platform_result = (
            {"delivery_mode": DEFERRED_PASSIVE_PENDING_MODE} if command.suppress_delivery else None
        )
        delivery_ids = [
            self._create_formal_delivery(
                session,
                case=case,
                entry=entry,
                destination_type=destination_type,
                destination_address=destination_address,
                waiting_on_after_delivery=wait_after,
                delivery_status=delivery_status,
                item_status=item_status,
                item_platform_result=item_platform_result,
            )
        ]
        if side is EntrySide.DEV and case.current_consultant_id is not None:
            consultant = session.get(User, case.current_consultant_id)
            if consultant is not None and consultant.active:
                delivery_ids.append(
                    self._create_formal_delivery(
                        session,
                        case=case,
                        entry=entry,
                        destination_type=DeliveryDestination.USER,
                        destination_address=consultant.wecom_userid,
                        waiting_on_after_delivery=None,
                    )
                )
        if mark_processing:
            case.status = CaseStatus.IN_PROGRESS
            self._append_entry(
                session,
                case=case,
                kind=CaseEntryKind.STATUS_CHANGED,
                side=EntrySide.SYSTEM,
                actor_user=actor_user,
                actor_name=actor_name,
                metadata={
                    "from_status": previous_status.value,
                    "to_status": CaseStatus.IN_PROGRESS.value,
                },
            )
        case.version += 1
        return self._result(case, entry.id, delivery_ids=tuple(delivery_ids))

    def _transfer_consultant(
        self, session: Session, command: TransferConsultant, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_case_transfer_authorized(session, actor.user_id, case)
        actor_user = self._directory.get_user(session, actor.user_id)
        new_consultant = self._directory.assert_consultant_in_queue(
            session, consultant_id=command.new_consultant_id, queue_id=case.consult_queue_id
        )
        if case.current_consultant_id == new_consultant.id:
            raise ValidationError("该人员已经是当前咨询经办人")
        old_consultant = self._user_name(session, case.current_consultant_id)
        case.current_consultant_id = new_consultant.id
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.TRANSFER_CONSULTANT,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "from_consultant": old_consultant,
                "to_consultant": new_consultant.display_name,
            },
        )
        consult_channel = self._directory.active_consult_channel(session, case.consult_queue_id)
        content = (
            f"咨询经办已由{old_consultant or '未指定'}转交给"
            f"{new_consultant.display_name}。"
            f"请后续由{new_consultant.display_name}继续跟进。"
        )
        delivery_ids = [
            self._create_notice_delivery(
                session,
                entry=entry,
                case=case,
                destination_type=DeliveryDestination.CHAT,
                destination_address=consult_channel.chatid,
                content=content,
            )
        ]
        delivery_ids.append(
            self._create_notice_delivery(
                session,
                entry=entry,
                case=case,
                destination_type=DeliveryDestination.USER,
                destination_address=new_consultant.wecom_userid,
                content=content,
            )
        )
        return self._result(case, entry.id, delivery_ids=tuple(delivery_ids))

    def _transfer_dev_team(
        self, session: Session, command: TransferDevTeam, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_dev_team_transfer_authorized(session, actor.user_id, case)
        actor_user = self._directory.get_user(session, actor.user_id)
        if case.current_dev_team_id == command.new_dev_team_id:
            raise ValidationError("目标研发责任团队与当前团队相同")
        old_channel = self._directory.active_channel(session, case.current_dev_team_id)
        new_channel = self._directory.active_channel(session, command.new_dev_team_id)
        old_team_id = case.current_dev_team_id
        old_team = session.get(Team, old_team_id)
        new_team = session.get(Team, new_channel.team_id)
        case.current_dev_team_id = new_channel.team_id
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.TRANSFER_DEV_TEAM,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "from_dev_team_id": str(old_team_id),
                "to_dev_team_id": str(new_channel.team_id),
                "from_dev_team": old_team.name if old_team else "未知团队",
                "to_dev_team": new_team.name if new_team else "未知团队",
            },
        )
        old_delivery = self._create_notice_delivery(
            session,
            entry=entry,
            case=case,
            destination_type=DeliveryDestination.CHAT,
            destination_address=old_channel.chatid,
            content="本问题已转出至另一研发责任团队，后续更新不再发送到本群。",
        )
        new_delivery = self._create_notice_delivery(
            session,
            entry=entry,
            case=case,
            destination_type=DeliveryDestination.CHAT,
            destination_address=new_channel.chatid,
            content="本问题已转入本团队，请在问题中心查看完整时间线。",
        )
        return self._result(case, entry.id, delivery_ids=(old_delivery, new_delivery))

    def _close_case(self, session: Session, command: CloseCase, actor: Actor) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_consult_manager(session, actor.user_id, case)
        self._assert_open(case)
        dev_channel = self._directory.active_channel(session, case.current_dev_team_id)
        actor_user = self._directory.get_user(session, actor.user_id)
        case.lifecycle_status = LifecycleStatus.CLOSED
        case.status = CaseStatus.CLOSED
        case.waiting_on = WaitingOn.NONE
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.CLOSED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={"action": "close_case"},
        )
        delivery_id = self._create_notice_delivery(
            session,
            entry=entry,
            case=case,
            destination_type=DeliveryDestination.CHAT,
            destination_address=dev_channel.chatid,
            content="咨询侧已确认问题闭环并关闭本问题。",
        )
        return self._result(case, entry.id, delivery_ids=(delivery_id,))

    def _set_case_status(
        self, session: Session, command: SetCaseStatus, actor: Actor
    ) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_case_visible(session, actor.user_id, case)
        self._assert_open(case)
        if command.status is CaseStatus.CLOSED:
            raise ValidationError("请通过‘确认客户侧闭环并关闭’操作关闭问题")
        if command.status is CaseStatus.PENDING_CONFIRMATION:
            raise ValidationError("待确定是初始状态，不能通过操作切回")
        if command.status is CaseStatus.WAITING_CUSTOMER:
            self._directory.assert_consult_manager(session, actor.user_id, case)
        if case.status is command.status:
            return self._result(case, None, idempotent=True)

        if command.status is CaseStatus.IN_PROGRESS:
            if case.status not in (
                CaseStatus.PENDING_CONFIRMATION,
                CaseStatus.WAITING_CUSTOMER,
                CaseStatus.SUSPENDED,
            ):
                raise ValidationError("只有待确定、待客户反馈或挂起状态可以受理")
            if case.status is CaseStatus.PENDING_CONFIRMATION and not self._directory.is_member(
                session, user_id=actor.user_id, team_id=case.current_dev_team_id
            ):
                raise Forbidden("待确定状态只能由研发团队成员受理")
        elif command.status is CaseStatus.WAITING_CUSTOMER:
            if case.status is not CaseStatus.IN_PROGRESS:
                raise ValidationError("只有处理中问题可以设为待客户反馈")
        elif command.status is CaseStatus.SUSPENDED:
            if case.status is not CaseStatus.IN_PROGRESS:
                raise ValidationError("只有处理中问题可以挂起")

        actor_user = self._directory.get_user(session, actor.user_id)
        previous_status = case.status
        case.status = command.status
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.STATUS_CHANGED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={
                "from_status": previous_status.value,
                "to_status": command.status.value,
            },
        )
        return self._result(case, entry.id)

    def _reopen_case(self, session: Session, command: ReopenCase, actor: Actor) -> CommandResult:
        case = self._load_case(session, command.case_ref, lock=True)
        self._assert_expected_version(case, command.expected_version)
        self._directory.assert_consult_manager(session, actor.user_id, case)
        if case.lifecycle_status is not LifecycleStatus.CLOSED:
            raise ValidationError("只有已关闭问题可以重新打开")
        if command.waiting_on is WaitingOn.NONE:
            raise ValidationError("重新打开问题时必须明确下一步责任方")
        actor_user = self._directory.get_user(session, actor.user_id)
        channel: TeamChannel | None = None
        if command.waiting_on is WaitingOn.DEV:
            channel = self._directory.active_channel(session, case.current_dev_team_id)
        case.lifecycle_status = LifecycleStatus.OPEN
        case.status = CaseStatus.IN_PROGRESS
        case.waiting_on = command.waiting_on
        case.version += 1
        entry = self._append_entry(
            session,
            case=case,
            kind=CaseEntryKind.REOPENED,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            metadata={"waiting_on": command.waiting_on.value},
        )
        delivery_ids: tuple[UUID, ...] = ()
        if channel is not None:
            delivery_id = self._create_notice_delivery(
                session,
                entry=entry,
                case=case,
                destination_type=DeliveryDestination.CHAT,
                destination_address=channel.chatid,
                content="咨询侧已重新打开本问题，请研发继续处理。",
            )
            delivery_ids = (delivery_id,)
        return self._result(case, entry.id, delivery_ids=delivery_ids)

    def _correct_entry(
        self, session: Session, command: CorrectEntry, actor: Actor
    ) -> CommandResult:
        if normalize_case_ref(command.case_ref) == normalize_case_ref(command.target_case_ref):
            raise ValidationError("纠错目标必须是另一个问题")
        cases = self._load_cases_for_update(session, command.case_ref, command.target_case_ref)
        source = cases[normalize_case_ref(command.case_ref)]
        target = cases[normalize_case_ref(command.target_case_ref)]
        self._assert_expected_version(source, command.expected_version)
        self._assert_expected_version(target, command.target_expected_version)
        self._directory.assert_consult_manager(session, actor.user_id, source)
        self._directory.assert_case_visible(session, actor.user_id, target)
        self._assert_open(target)
        actor_user = self._directory.get_user(session, actor.user_id)
        original = session.scalar(
            select(CaseEntry)
            .where(CaseEntry.id == command.entry_id, CaseEntry.case_id == source.id)
            .with_for_update()
        )
        if original is None:
            raise NotFound("待纠正的时间线记录不存在")
        if original.kind is not CaseEntryKind.FORMAL_MESSAGE:
            raise ValidationError("只能纠正正式消息")
        original_parts = self._entry_parts(session, original.id)
        if not original_parts:
            raise ValidationError("该正式消息没有可纠正的内容")
        copied_parts = tuple(self._part_from_orm(part) for part in original_parts)
        source.version += 1
        target.version += 1
        source_entry = self._append_entry(
            session,
            case=source,
            kind=CaseEntryKind.CORRECTION,
            side=EntrySide.SYSTEM,
            actor_user=actor_user,
            corrects_entry_id=original.id,
            metadata={"corrected_to_case_ref": target.case_ref, "action": "mark_misrouted"},
        )
        corrected_entry = self._append_entry(
            session,
            case=target,
            kind=CaseEntryKind.CORRECTION,
            side=original.side,
            actor_user=actor_user,
            intent=original.message_intent,
            corrects_entry_id=original.id,
            metadata={
                "corrected_from_case_ref": source.case_ref,
                "action": "copy_misrouted_message",
                "original_actor_name": original.actor_name_snapshot,
            },
        )
        self._add_parts(session, corrected_entry, copied_parts)
        source_destination_type, source_destination_address = self._destination_for_side(
            session, source, original.side
        )
        source_delivery = self._create_notice_delivery(
            session,
            entry=source_entry,
            case=source,
            destination_type=source_destination_type,
            destination_address=source_destination_address,
            content="上一条正式消息已被标记为误发；请以问题中心中的纠错记录为准。",
        )
        target_destination_type, target_destination_address = self._destination_for_side(
            session, target, original.side
        )
        wait_after = (
            self._opposite_waiting_side(original.side)
            if original.message_intent is MessageIntent.HANDOFF
            else None
        )
        corrected_delivery = self._create_formal_delivery(
            session,
            case=target,
            entry=corrected_entry,
            destination_type=target_destination_type,
            destination_address=target_destination_address,
            waiting_on_after_delivery=wait_after,
        )
        return self._result(
            source,
            source_entry.id,
            delivery_ids=(source_delivery, corrected_delivery),
        )

    def _validate_parts(self, parts: Sequence[ContentPart]) -> tuple[ContentPart, ...]:
        if not parts:
            raise ValidationError("正式消息至少需要一个文字或图片片段")
        normalized: list[ContentPart] = []
        has_nonempty_text = False
        for part in parts:
            if isinstance(part, TextPart):
                text = part.text.strip()
                if not text:
                    raise ValidationError("文字片段不能为空")
                if len(text) > 20_000:
                    raise ValidationError("单个文字片段不能超过 20000 个字符")
                normalized.append(TextPart(text=text))
                has_nonempty_text = True
            elif isinstance(part, ImagePart):
                normalized.append(part)
            else:
                raise ValidationError("只支持文字和图片片段")
        # Standalone image is deliberately outside the MVP.  An image-first
        # mixed message remains valid as long as it includes a text fragment.
        if not has_nonempty_text:
            raise ValidationError("MVP 的正式消息必须至少包含一个文字片段")
        return tuple(normalized)

    def _assert_media_exists(self, session: Session, parts: Sequence[ContentPart]) -> None:
        media_ids = {part.media_id for part in parts if isinstance(part, ImagePart)}
        if not media_ids:
            return
        existing_ids = set(
            session.scalars(select(StoredMedia.id).where(StoredMedia.id.in_(media_ids))).all()
        )
        if existing_ids != media_ids:
            raise ValidationError("消息引用了不存在的已转存图片")

    def _validated_title(self, title: str, parts: Sequence[ContentPart]) -> str:
        normalized = title.strip()
        if not normalized:
            first_text = next((part.text for part in parts if isinstance(part, TextPart)), "")
            normalized = first_text[:120]
        if not normalized:
            raise ValidationError("问题标题不能为空")
        if len(normalized) > 512:
            raise ValidationError("问题标题不能超过 512 个字符")
        return normalized

    def _validated_customer_name(self, name: str | None) -> str:
        return (name or "").strip() or "未录入"

    def _validated_customer_group_name(self, name: str | None) -> str | None:
        if name is None:
            return None
        if not isinstance(name, str):
            raise ValidationError("客户群聊名称必须是文本")
        normalized = name.strip()
        if len(normalized) > 256:
            raise ValidationError("客户群聊名称不能超过 256 个字符")
        return normalized or None

    def _validated_handler_name(self, name: str) -> str:
        if not isinstance(name, str):
            raise ValidationError("咨询经办人必须是文本")
        normalized = name.strip()
        if not normalized:
            raise ValidationError("咨询经办人不能为空")
        if len(normalized) > 256:
            raise ValidationError("咨询经办人不能超过 256 个字符")
        return normalized

    def _validated_related_case(
        self, session: Session, related_case_id: UUID | None
    ) -> UUID | None:
        if related_case_id is None:
            return None
        if session.get(Case, related_case_id) is None:
            raise NotFound("关联问题不存在")
        return related_case_id

    def _append_entry(
        self,
        session: Session,
        *,
        case: Case,
        kind: CaseEntryKind,
        side: EntrySide,
        actor_user: User | None,
        actor_name: str | None = None,
        intent: MessageIntent | None = None,
        source_msgid: str | None = None,
        corrects_entry_id: UUID | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> CaseEntry:
        if actor_user is None and not actor_name:
            raise ValidationError("问题记录缺少操作人名称")
        case.last_entry_sequence += 1
        entry = CaseEntry(
            id=uuid4(),
            case_id=case.id,
            sequence=case.last_entry_sequence,
            kind=kind,
            side=side,
            actor_user_id=actor_user.id if actor_user is not None else None,
            actor_name_snapshot=actor_name or actor_user.display_name,
            message_intent=intent,
            source_msgid=source_msgid,
            corrects_entry_id=corrects_entry_id,
            metadata_json=dict(metadata or {}),
        )
        session.add(entry)
        return entry

    def _add_parts(self, session: Session, entry: CaseEntry, parts: Sequence[ContentPart]) -> None:
        for position, part in enumerate(parts):
            if isinstance(part, TextPart):
                record = CaseEntryPart(
                    id=uuid4(),
                    entry_id=entry.id,
                    position=position,
                    kind=PartKind.TEXT,
                    text=part.text,
                    media_id=None,
                )
            else:
                record = CaseEntryPart(
                    id=uuid4(),
                    entry_id=entry.id,
                    position=position,
                    kind=PartKind.IMAGE,
                    text=None,
                    media_id=part.media_id,
                )
            session.add(record)

    def _ensure_bug_number(self, session: Session, case: Case) -> str:
        """Assign a number to legacy cases before rendering a human-facing message."""
        if not case.bug_number:
            case.bug_number = next_bug_number(session, _as_utc(case.created_at))
        assert case.bug_number is not None
        return case.bug_number

    def _load_case(self, session: Session, case_ref: str, *, lock: bool = False) -> Case:
        normalized = normalize_case_ref(case_ref)
        statement = select(Case).where(Case.case_ref == normalized)
        if lock:
            statement = statement.with_for_update()
        case = session.scalar(statement)
        if case is None:
            raise NotFound("问题不存在")
        return case

    def _load_cases_for_update(self, session: Session, *case_refs: str) -> dict[str, Case]:
        normalized_refs = tuple(normalize_case_ref(case_ref) for case_ref in case_refs)
        cases = session.scalars(
            select(Case)
            .where(Case.case_ref.in_(normalized_refs))
            .order_by(Case.case_ref.asc())
            .with_for_update()
        ).all()
        by_ref = {case.case_ref: case for case in cases}
        if set(by_ref) != set(normalized_refs):
            raise NotFound("一个或多个问题不存在")
        return by_ref

    def _case_by_id(self, session: Session, case_id: UUID) -> Case:
        case = session.get(Case, case_id)
        if case is None:
            raise NotFound("问题不存在")
        return case

    def _existing_source_entry(
        self, session: Session, source_msgid: str | None
    ) -> CaseEntry | None:
        cleaned = self._clean_source_msgid(source_msgid)
        if cleaned is None:
            return None
        return session.scalar(select(CaseEntry).where(CaseEntry.source_msgid == cleaned))

    def _new_unique_case_ref(self, session: Session) -> str:
        for _ in range(16):
            candidate = new_case_ref()
            if (
                session.scalar(select(Case.id).where(Case.case_ref == candidate)) is None
                and session.scalar(
                    select(IssueIntakeDraft.id).where(IssueIntakeDraft.issue_number == candidate)
                )
                is None
            ):
                return candidate
        raise Conflict("无法生成未冲突的问题引用标记")

    def _assert_expected_version(self, case: Case, expected_version: int | None) -> None:
        if expected_version is None:
            return
        if expected_version < 1:
            raise ValidationError("问题版本号无效")
        if case.version != expected_version:
            raise Conflict("问题已被其他人更新，请刷新后重新确认")

    def _has_consult_deadline_access(self, session: Session, actor_id: UUID, case: Case) -> bool:
        return self._directory.is_global_admin(session, actor_id) or self._directory.is_member(
            session, user_id=actor_id, team_id=case.consult_queue_id
        )

    def _assert_consult_deadline_access(self, session: Session, actor_id: UUID, case: Case) -> None:
        if not self._has_consult_deadline_access(session, actor_id, case):
            raise Forbidden("只有咨询队列成员可以调整问题期限")

    def _assert_open(self, case: Case) -> None:
        if case.lifecycle_status is not LifecycleStatus.OPEN:
            raise ValidationError("已关闭问题不能继续发送正式消息")

    def _clean_source_msgid(self, source_msgid: str | None) -> str | None:
        if source_msgid is None:
            return None
        cleaned = source_msgid.strip()
        if not cleaned:
            return None
        if len(cleaned) > 256:
            raise ValidationError("消息标识过长")
        return cleaned

    def _entry_parts(self, session: Session, entry_id: UUID) -> list[CaseEntryPart]:
        return session.scalars(
            select(CaseEntryPart)
            .where(CaseEntryPart.entry_id == entry_id)
            .order_by(CaseEntryPart.position.asc())
        ).all()

    def _part_from_orm(self, part: CaseEntryPart) -> ContentPart:
        if part.kind is PartKind.TEXT:
            assert part.text is not None
            return TextPart(part.text)
        assert part.media_id is not None
        return ImagePart(part.media_id)

    def _user_name(self, session: Session, user_id: UUID | None) -> str | None:
        if user_id is None:
            return None
        user = self._directory.get_user(session, user_id, require_active=False)
        return user.display_name

    def _result(
        self,
        case: Case,
        entry_id: UUID | None,
        *,
        delivery_ids: tuple[UUID, ...] = (),
        idempotent: bool = False,
    ) -> CommandResult:
        return CommandResult(
            case_ref=case.case_ref,
            case_version=case.version,
            entry_id=entry_id,
            delivery_ids=delivery_ids,
            idempotent=idempotent,
        )
