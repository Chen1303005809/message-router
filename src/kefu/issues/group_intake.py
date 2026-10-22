"""Group-scoped, multi-turn Agent intake for new consultation issues."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kefu.agent.service import Agent
from kefu.case_desk.contracts import (
    ContentPart,
    CreateCase,
    GroupActor,
    ImagePart,
    TextPart,
)
from kefu.case_desk.errors import CaseDeskError, RoutingUnavailable
from kefu.issues.service import IssueManagement, map_image_placeholders
from kefu.media.storage import MediaIngestor, ObjectStorage
from kefu.persistence.models import (
    Case,
    CaseEntry,
    CaseEntryPart,
    EntrySide,
    GroupIssueIntakeSession,
    GroupIssueIntakeState,
    GroupIssueIntakeTurn,
    IssueModule,
    PartKind,
    StoredMedia,
    Team,
    TeamKind,
)
from kefu.routing.directory import DatabaseRoutingDirectory
from kefu.wecom.transport import InboundEvent, InboundImagePart, InboundTextPart

logger = logging.getLogger(__name__)

CONFIRM_PHRASE = "确认派发"
INTAKE_MARKER_PREFIX = "〔KF-INTAKE:"
_INTAKE_MARKER = re.compile(r"〔KF-INTAKE:([0-9a-fA-F]{32})〕")
_AGENT_FAILURE_REPLY = (
    "Agent 暂时无法整理本轮内容，尚未派发。请引用本条提案并 @机器人，"
    "重新发送本轮修改；如果无需修改，请回复“确认派发”重试。"
)
_PENDING_REPLY = "这条消息正在处理中，请稍候再引用本条提案继续。"

_FIELD_KEYS = (
    "issue_title",
    "issue_description",
    "acceptance_team_id",
    "suspected_type",
    "current_situation",
    "customer_name",
    "customer_contact_name",
    "customer_contact_method",
)


class GroupIssueIntakeService:
    """Own the durable proposal lifecycle and the one authorized dispatch path."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        issues: IssueManagement,
        agent: Agent,
        media_ingestor: MediaIngestor,
        storage: ObjectStorage,
        *,
        session_ttl: timedelta = timedelta(hours=2),
        directory: DatabaseRoutingDirectory | None = None,
    ) -> None:
        if session_ttl <= timedelta(0):
            raise ValueError("群聊受理会话有效期必须大于零")
        self._session_factory = session_factory
        self._issues = issues
        self._agent = agent
        self._media_ingestor = media_ingestor
        self._storage = storage
        self._session_ttl = session_ttl
        self._directory = directory or DatabaseRoutingDirectory()

    @staticmethod
    def should_handle(event: InboundEvent, origin_side: EntrySide | None) -> bool:
        """Select a new consult-group intake or a reply quoting its marker."""
        if event.chattype != "group" or not event.mentioned_bot:
            return False
        quote = event.quote_content or ""
        if INTAKE_MARKER_PREFIX in quote:
            return True
        return not quote.strip() and origin_side is EntrySide.CONSULT

    async def handle(self, event: InboundEvent) -> str:
        """Handle one eligible event and return its immediate group response."""
        if event.chattype != "group" or not event.mentioned_bot:
            return "请在已绑定的咨询群中 @机器人 开始或继续问题受理。"
        decode_error = event.metadata.get("decode_error")
        if isinstance(decode_error, str) and decode_error:
            return decode_error
        if not event.chatid or not event.chatid.strip():
            return "消息缺少群聊标识，无法开始问题受理。"
        quote = event.quote_content or ""
        if INTAKE_MARKER_PREFIX in quote:
            matches = _INTAKE_MARKER.findall(quote)
            if len(matches) != 1 or quote.count(INTAKE_MARKER_PREFIX) != 1:
                return "提案标记无效或不唯一。请引用一条有效的机器人提案并 @机器人。"
            return await self._handle_followup(event, matches[0].lower())
        if quote.strip():
            return "继续群聊问题受理时，请引用包含会话标记的机器人提案并 @机器人。"
        return await self._handle_initial(event)

    async def cleanup_expired(self, now: datetime | None = None) -> int:
        """Remove expired sessions and their images when no issue uses them."""
        return await asyncio.to_thread(self._cleanup_expired, now or _utc_now())

    async def _handle_initial(self, event: InboundEvent) -> str:
        try:
            prepared = await asyncio.to_thread(self._create_initial_session, event)
        except (CaseDeskError, ValueError) as error:
            return str(error)
        except Exception:
            logger.exception("group_issue_intake_start_failed msgid=%s", event.msgid)
            return "问题受理暂时无法启动，请稍后在群里 @机器人 重试。"
        if isinstance(prepared, str):
            return prepared
        session_id, turn_id, token = prepared
        if turn_id is None:
            return await asyncio.to_thread(self._saved_or_pending_reply, session_id, event.msgid)
        return await self._process_turn(session_id, turn_id, token, should_confirm=False)

    async def _handle_followup(self, event: InboundEvent, token: str) -> str:
        try:
            prepared = await asyncio.to_thread(self._create_followup_turn, event, token)
        except (CaseDeskError, ValueError) as error:
            return str(error)
        except Exception:
            logger.exception("group_issue_intake_turn_reserve_failed msgid=%s", event.msgid)
            return "本轮消息暂时无法保存，请稍后引用提案并 @机器人 重试。"
        if isinstance(prepared, str):
            return prepared
        session_id, turn_id, saved_reply, state = prepared
        if state is GroupIssueIntakeState.COMPLETED:
            reply = await asyncio.to_thread(self._completed_reply, session_id)
            if turn_id is not None:
                await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
            return reply
        if saved_reply is not None:
            return saved_reply
        if turn_id is None:
            return _PENDING_REPLY
        user_text = await asyncio.to_thread(self._turn_text, turn_id)
        should_confirm = CONFIRM_PHRASE in user_text
        return await self._process_turn(session_id, turn_id, token, should_confirm=should_confirm)

    def _create_initial_session(
        self, event: InboundEvent
    ) -> tuple[UUID, UUID | None, str] | str:
        msgid = _validated_msgid(event.msgid)
        with self._session_factory() as session:
            route = self._directory.active_team_for_chatid(session, event.chatid or "")
            if route.kind is not TeamKind.CONSULT_QUEUE:
                return "只有已绑定的咨询队列群可以发起新问题。"
            existing = session.scalar(
                select(GroupIssueIntakeSession).where(
                    GroupIssueIntakeSession.source_msgid == msgid
                )
            )
            if existing is not None:
                if existing.chatid != event.chatid:
                    return "该消息标识已用于其他群聊，不能重复受理。"
                if existing.consult_queue_id != route.id:
                    return "该受理会话对应的咨询群绑定已变化，不能重复受理。"
                return existing.id, None, existing.token

        if not any(
            isinstance(part, InboundTextPart) and part.text.strip()
            for part in event.parts
        ):
            return "请在 @机器人 消息中附上文字描述；图片会保留为附件，但 Agent 不会识别图片内容。"

        raw_parts, plain_text = self._ingest_initial_parts(event)
        now = _utc_now()
        token = uuid4().hex
        session_id = uuid4()
        turn_id = uuid4()
        try:
            with self._session_factory() as session, session.begin():
                route = self._directory.active_team_for_chatid(session, event.chatid or "")
                if route.kind is not TeamKind.CONSULT_QUEUE:
                    raise RoutingUnavailable("只有已绑定的咨询队列群可以发起新问题。")
                session.add(
                    GroupIssueIntakeSession(
                        id=session_id,
                        token=token,
                        chatid=event.chatid or "",
                        consult_queue_id=route.id,
                        source_msgid=msgid,
                        raw_text=plain_text,
                        parts_json=raw_parts,
                        fields_json={key: "" for key in _FIELD_KEYS},
                        state=GroupIssueIntakeState.ACTIVE,
                        expires_at=now + self._session_ttl,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    GroupIssueIntakeTurn(
                        id=turn_id,
                        session_id=session_id,
                        msgid=msgid,
                        user_text=plain_text,
                        created_at=now,
                    )
                )
            return session_id, turn_id, token
        except IntegrityError:
            self._delete_orphan_media(
                UUID(item["media_id"])
                for item in raw_parts
                if item.get("kind") == PartKind.IMAGE.value
            )
            # A concurrent duplicate callback won the unique message key.
            with self._session_factory() as session:
                existing = session.scalar(
                    select(GroupIssueIntakeSession).where(
                        GroupIssueIntakeSession.source_msgid == msgid
                    )
                )
                if existing is not None and existing.chatid == event.chatid:
                    return existing.id, None, existing.token
            raise
        except Exception:
            self._delete_orphan_media(
                UUID(item["media_id"])
                for item in raw_parts
                if item.get("kind") == PartKind.IMAGE.value
            )
            raise

    def _ingest_initial_parts(self, event: InboundEvent) -> tuple[list[dict[str, str]], str]:
        parts: list[dict[str, str]] = []
        text_fragments: list[str] = []
        try:
            for part in event.parts:
                if isinstance(part, InboundTextPart):
                    if part.text.strip():
                        parts.append({"kind": "text", "text": part.text})
                        text_fragments.append(part.text)
                elif isinstance(part, InboundImagePart):
                    media_id = self._media_ingestor.ingest(part)
                    parts.append({"kind": "image", "media_id": str(media_id)})
                    text_fragments.append("[图片]")
        except Exception:
            self._delete_orphan_media(
                UUID(item["media_id"])
                for item in parts
                if item.get("kind") == PartKind.IMAGE.value
            )
            raise
        return parts, "".join(text_fragments).strip()

    def _create_followup_turn(
        self, event: InboundEvent, token: str
    ) -> tuple[UUID, UUID | None, str | None, GroupIssueIntakeState] | str:
        msgid = _validated_msgid(event.msgid)
        now = _utc_now()
        with self._session_factory() as session, session.begin():
            record = session.scalar(
                select(GroupIssueIntakeSession)
                .where(GroupIssueIntakeSession.token == token)
                .with_for_update()
            )
            if record is None:
                return "找不到这条受理提案。请在已绑定的咨询群中 @机器人 重新发送问题。"
            if record.chatid != event.chatid:
                return "该提案属于其他群聊，不能跨群继续。请在原咨询群引用提案并 @机器人。"
            if _as_utc(record.expires_at) <= now:
                return "这条问题受理会话已过期（两小时有效），请在群里 @机器人 重新发送问题。"
            route = self._directory.active_team_for_chatid(session, record.chatid)
            if route.kind is not TeamKind.CONSULT_QUEUE or route.id != record.consult_queue_id:
                return "原咨询群绑定已变化或失效，不能继续此提案。请确认群绑定后重新发起。"
            existing = session.scalar(
                select(GroupIssueIntakeTurn).where(GroupIssueIntakeTurn.msgid == msgid)
            )
            if existing is not None:
                if existing.session_id != record.id:
                    return "该消息标识已用于其他受理会话。"
                return record.id, None, existing.reply_text, record.state
            user_text = _event_text(event)
            turn = GroupIssueIntakeTurn(
                id=uuid4(),
                session_id=record.id,
                msgid=msgid,
                user_text=user_text,
                created_at=now,
            )
            session.add(turn)
            session.flush()
            return record.id, turn.id, None, record.state

    async def _process_turn(
        self,
        session_id: UUID,
        turn_id: UUID,
        token: str,
        *,
        should_confirm: bool,
    ) -> str:
        try:
            context = await asyncio.to_thread(self._processing_context, session_id, turn_id)
        except CaseDeskError as error:
            reply = str(error)
            await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
            return reply
        except Exception:
            logger.exception("group_issue_intake_context_failed session_id=%s", session_id)
            await asyncio.to_thread(self._save_turn_reply, turn_id, _AGENT_FAILURE_REPLY)
            return _AGENT_FAILURE_REPLY
        if context is None:
            return _PENDING_REPLY
        if context.get("expired"):
            reply = (
                "这条问题受理会话已过期（两小时有效），尚未派发。"
                "请在群里 @机器人 重新发送问题。"
            )
            await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
            return reply
        state = context["state"]
        if state is GroupIssueIntakeState.COMPLETED:
            reply = await asyncio.to_thread(self._completed_reply, session_id)
            await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
            return reply
        if state is GroupIssueIntakeState.DISPATCHING:
            if _without_confirm(context["latest_user_text"]).strip():
                recovered_issue_id = await asyncio.to_thread(
                    self._recover_or_reopen_dispatch, session_id
                )
                if recovered_issue_id is not None:
                    reply = await asyncio.to_thread(self._completed_reply, session_id)
                    await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
                    return reply
                try:
                    context = await asyncio.to_thread(
                        self._processing_context, session_id, turn_id
                    )
                except CaseDeskError as error:
                    reply = str(error)
                    await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
                    return reply
                if context is None:
                    return _PENDING_REPLY
                if context.get("expired"):
                    reply = (
                        "这条问题受理会话已过期（两小时有效），尚未派发。"
                        "请在群里 @机器人 重新发送问题。"
                    )
                    await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
                    return reply
                if context["state"] is GroupIssueIntakeState.COMPLETED:
                    reply = await asyncio.to_thread(self._completed_reply, session_id)
                    await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
                    return reply
            else:
                return await self._dispatch_confirmed(session_id, turn_id, token)

        fields = context["fields"]
        latest = context["latest_user_text"]
        confirmation_only = should_confirm and not _without_confirm(latest).strip()
        retry_after_failure = context["retry_after_agent_failure"]
        needs_agent = not confirmation_only or retry_after_failure
        if needs_agent:
            current_team_ids = {item["id"] for item in context["teams"]}
            if fields.get("acceptance_team_id") not in current_team_ids:
                fields["acceptance_team_id"] = ""
            conversation = context["conversation"]
            if should_confirm and conversation:
                last = dict(conversation[-1])
                last["content"] = _without_confirm(str(last.get("content", "")))
                conversation[-1] = last
            try:
                updated = await self._agent.draft_group_issue(
                    context["original_text"],
                    conversation,
                    fields,
                    context["teams"],
                    request_id=f"group-intake-{turn_id.hex}",
                )
            except Exception:
                logger.exception("group_issue_intake_agent_failed session_id=%s", session_id)
                reply = self._proposal_reply(
                    token,
                    fields,
                    context["teams"],
                    extra="Agent 暂时不可用，本轮内容尚未应用，也没有派发。请引用本条提案重试。",
                )
                await asyncio.to_thread(
                    self._save_proposal_and_reply, session_id, turn_id, fields, reply
                )
                return reply
            fields = _normalize_fields(updated)

        reply = self._proposal_reply(token, fields, context["teams"])
        missing = _missing_fields(fields, context["teams"])
        if should_confirm and not missing:
            try:
                await asyncio.to_thread(self._save_proposal, session_id, turn_id, fields)
            except Exception:
                logger.exception(
                    "group_issue_intake_proposal_save_failed session_id=%s", session_id
                )
                await asyncio.to_thread(self._save_turn_reply, turn_id, _AGENT_FAILURE_REPLY)
                return _AGENT_FAILURE_REPLY
            return await self._dispatch_confirmed(session_id, turn_id, token)

        if should_confirm and missing:
            reply += "\n\n当前仍有未补齐项，暂未派发。请先补充后再次回复“确认派发”。"
        await asyncio.to_thread(self._save_proposal_and_reply, session_id, turn_id, fields, reply)
        return reply

    def _processing_context(self, session_id: UUID, turn_id: UUID) -> dict[str, Any] | None:
        now = _utc_now()
        with self._session_factory() as session:
            record = session.get(GroupIssueIntakeSession, session_id)
            turn = session.get(GroupIssueIntakeTurn, turn_id)
            if record is None or turn is None:
                return None
            if _as_utc(record.expires_at) <= now:
                return {
                    "state": GroupIssueIntakeState.ACTIVE,
                    "expired": True,
                    "fields": {},
                    "teams": [],
                    "conversation": [],
                    "original_text": "",
                    "latest_user_text": turn.user_text,
                    "retry_after_agent_failure": False,
                }
            route = self._directory.active_team_for_chatid(session, record.chatid)
            if route.kind is not TeamKind.CONSULT_QUEUE or route.id != record.consult_queue_id:
                raise RoutingUnavailable("原咨询群绑定已变化或失效，不能继续此提案。")
            team_rows = self._routable_teams(session)
            teams = [{"id": str(team.id), "name": team.name} for team in team_rows]
            fields = _normalize_fields(record.fields_json)
            team_ids = {item["id"] for item in teams}
            if fields.get("acceptance_team_id") not in team_ids:
                fields["acceptance_team_id"] = ""
            turns = session.scalars(
                select(GroupIssueIntakeTurn)
                .where(GroupIssueIntakeTurn.session_id == record.id)
                .order_by(GroupIssueIntakeTurn.created_at.asc(), GroupIssueIntakeTurn.id.asc())
            ).all()
            conversation: list[dict[str, str]] = []
            for item in turns:
                text = _without_confirm(item.user_text)
                conversation.append({"role": "user", "content": text})
                if item.reply_text:
                    conversation.append({"role": "assistant", "content": item.reply_text})
            previous_turns = [item for item in turns if item.id != turn_id]
            retry_after_agent_failure = bool(
                previous_turns
                and previous_turns[-1].reply_text
                and "Agent 暂时" in previous_turns[-1].reply_text
            )
            return {
                "state": record.state,
                "fields": fields,
                "teams": teams,
                "conversation": conversation,
                "original_text": record.raw_text,
                "latest_user_text": turn.user_text,
                "retry_after_agent_failure": retry_after_agent_failure,
            }

    def _routable_teams(self, session: Session) -> list[Team]:
        candidates: list[Team] = []
        for team in self._directory.active_dev_teams(session):
            try:
                self._directory.active_channel(session, team.id)
            except RoutingUnavailable:
                continue
            candidates.append(team)
        return candidates

    async def _dispatch_confirmed(self, session_id: UUID, turn_id: UUID, token: str) -> str:
        try:
            proposal = await asyncio.to_thread(self._begin_dispatch, session_id, turn_id)
        except CaseDeskError as error:
            await asyncio.to_thread(self._reset_active, session_id)
            reply = f"无法派发：{error} 请修正提案后再次回复“确认派发”。"
            await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
            return reply
        if isinstance(proposal, str):
            await asyncio.to_thread(self._save_turn_reply, turn_id, proposal)
            return proposal
        if proposal is None:
            reply = await asyncio.to_thread(self._completed_reply, session_id)
            await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
            return reply
        command, chatid, issue_id = proposal
        if issue_id is not None:
            await asyncio.to_thread(self._mark_completed, session_id, issue_id)
            return await asyncio.to_thread(self._completed_reply, session_id)
        try:
            result = await asyncio.to_thread(
                self._issues.execute,
                command,
                GroupActor(EntrySide.CONSULT),
            )
            if result.case_ref is None:
                raise RuntimeError("问题创建没有返回问题编号")
            with self._session_factory() as session:
                issue = session.scalar(select(Case).where(Case.case_ref == result.case_ref))
                if issue is None:
                    raise RuntimeError("正式问题已创建但无法读取问题记录")
                issue_id = issue.id
            await asyncio.to_thread(self._mark_completed, session_id, issue_id)
            reply = await asyncio.to_thread(self._completed_reply, session_id)
            await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
            logger.info(
                "group_issue_intake_dispatched session_id=%s issue_ref=%s chatid_len=%s",
                session_id,
                result.case_ref,
                len(chatid),
            )
            return reply
        except Exception:
            logger.exception("group_issue_intake_dispatch_failed session_id=%s", session_id)
            recovered_issue_id = await asyncio.to_thread(
                self._recover_or_reopen_dispatch, session_id
            )
            if recovered_issue_id is not None:
                reply = await asyncio.to_thread(self._completed_reply, session_id)
                await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
                return reply
            reply = (
                "问题暂未成功派发。提案已保留；请引用本条提案并 @机器人，再次回复"
                "“确认派发”重试，系统会避免重复创建。"
            )
            await asyncio.to_thread(self._save_turn_reply, turn_id, reply)
            return reply

    def _begin_dispatch(
        self, session_id: UUID, turn_id: UUID
    ) -> tuple[CreateCase, str, UUID | None] | str | None:
        with self._session_factory() as session, session.begin():
            record = session.scalar(
                select(GroupIssueIntakeSession)
                .where(GroupIssueIntakeSession.id == session_id)
                .with_for_update()
            )
            turn = session.get(GroupIssueIntakeTurn, turn_id)
            if record is None or turn is None:
                return "找不到这条受理会话，无法派发。"
            if _as_utc(record.expires_at) <= _utc_now():
                return "这条问题受理会话已过期，尚未派发。请重新 @机器人 发起问题。"
            if record.state is GroupIssueIntakeState.COMPLETED:
                return None
            source_marker = f"group-intake:{record.id.hex}"
            existing_entry = session.scalar(
                select(CaseEntry).where(CaseEntry.source_msgid == source_marker)
            )
            if existing_entry is not None:
                record.state = GroupIssueIntakeState.COMPLETED
                record.issue_id = existing_entry.case_id
                return None
            route = self._directory.active_team_for_chatid(session, record.chatid)
            if route.kind is not TeamKind.CONSULT_QUEUE or route.id != record.consult_queue_id:
                record.state = GroupIssueIntakeState.ACTIVE
                return "原咨询群绑定已变化或失效，问题尚未派发。"
            fields = _normalize_fields(record.fields_json)
            candidate_teams = {str(team.id): team for team in self._routable_teams(session)}
            candidate_options = [
                {"id": key, "name": team.name}
                for key, team in candidate_teams.items()
            ]
            if _missing_fields(fields, candidate_options):
                record.state = GroupIssueIntakeState.ACTIVE
                return self._proposal_reply(
                    record.token,
                    fields,
                    candidate_options,
                    extra="必填信息不完整，尚未派发。请继续补充后再次回复“确认派发”。",
                )
            team_id = fields["acceptance_team_id"]
            team = candidate_teams.get(team_id)
            if team is None or not team.active:
                record.state = GroupIssueIntakeState.ACTIVE
                return "目标研发群已失效，尚未派发。请引用提案选择当前可用的研发群后再次确认。"
            source_parts = tuple(_part_from_json(item) for item in record.parts_json)
            image_ids = [
                part.media_id for part in source_parts if isinstance(part, ImagePart)
            ]
            parts = map_image_placeholders(fields["issue_description"].strip(), image_ids)
            command = CreateCase(
                title=fields["issue_title"],
                consult_queue_id=record.consult_queue_id,
                dev_team_id=team.id,
                initial_acceptance_team_id=team.id,
                customer_name=fields.get("customer_name") or None,
                customer_contact_name=fields.get("customer_contact_name") or None,
                customer_contact_method=fields.get("customer_contact_method") or None,
                parts=parts,
                source_msgid=source_marker,
                issue_description=fields["issue_description"],
                suspected_type=fields["suspected_type"],
                issue_current_situation=fields.get("current_situation") or None,
                issue_source="企业微信咨询群",
                issue_metadata={"intake_source_msgid": record.source_msgid},
                origin_chatid=record.chatid,
            )
            record.state = GroupIssueIntakeState.DISPATCHING
            record.fields_json = fields
            return command, record.chatid, None

    def _recover_or_reopen_dispatch(self, session_id: UUID) -> UUID | None:
        with self._session_factory() as session, session.begin():
            record = session.get(GroupIssueIntakeSession, session_id)
            if record is None:
                return None
            source_marker = f"group-intake:{record.id.hex}"
            entry = session.scalar(
                select(CaseEntry).where(CaseEntry.source_msgid == source_marker)
            )
            if entry is not None:
                record.state = GroupIssueIntakeState.COMPLETED
                record.issue_id = entry.case_id
                return entry.case_id
            record.state = GroupIssueIntakeState.ACTIVE
            return None

    def _reset_active(self, session_id: UUID) -> None:
        with self._session_factory() as session, session.begin():
            record = session.get(GroupIssueIntakeSession, session_id)
            if record is not None and record.state is GroupIssueIntakeState.DISPATCHING:
                record.state = GroupIssueIntakeState.ACTIVE

    def _save_proposal(
        self, session_id: UUID, turn_id: UUID, fields: Mapping[str, str]
    ) -> None:
        with self._session_factory() as session, session.begin():
            record = session.get(GroupIssueIntakeSession, session_id)
            turn = session.get(GroupIssueIntakeTurn, turn_id)
            if record is None or turn is None:
                return
            record.fields_json = _normalize_fields(fields)

    def _save_proposal_and_reply(
        self,
        session_id: UUID,
        turn_id: UUID,
        fields: Mapping[str, str],
        reply: str,
    ) -> None:
        with self._session_factory() as session, session.begin():
            record = session.get(GroupIssueIntakeSession, session_id)
            turn = session.get(GroupIssueIntakeTurn, turn_id)
            if record is None or turn is None:
                return
            record.fields_json = _normalize_fields(fields)
            turn.reply_text = reply

    def _save_turn_reply(self, turn_id: UUID, reply: str) -> None:
        with self._session_factory() as session, session.begin():
            turn = session.get(GroupIssueIntakeTurn, turn_id)
            if turn is not None:
                turn.reply_text = reply

    def _turn_text(self, turn_id: UUID) -> str:
        with self._session_factory() as session:
            turn = session.get(GroupIssueIntakeTurn, turn_id)
            return turn.user_text if turn is not None else ""

    def _saved_or_pending_reply(self, session_id: UUID, msgid: str) -> str:
        completed = False
        with self._session_factory() as session:
            record = session.get(GroupIssueIntakeSession, session_id)
            completed = bool(record and record.state is GroupIssueIntakeState.COMPLETED)
            turn = session.scalar(
                select(GroupIssueIntakeTurn).where(
                    GroupIssueIntakeTurn.session_id == session_id,
                    GroupIssueIntakeTurn.msgid == msgid,
                )
            )
            reply = turn.reply_text if turn and turn.reply_text else _PENDING_REPLY
        return self._completed_reply(session_id) if completed else reply

    def _mark_completed(self, session_id: UUID, issue_id: UUID) -> None:
        with self._session_factory() as session, session.begin():
            record = session.get(GroupIssueIntakeSession, session_id)
            if record is not None:
                record.state = GroupIssueIntakeState.COMPLETED
                record.issue_id = issue_id

    def _completed_reply(self, session_id: UUID) -> str:
        with self._session_factory() as session:
            record = session.get(GroupIssueIntakeSession, session_id)
            issue = session.get(Case, record.issue_id) if record and record.issue_id else None
            if issue is None:
                return "该提案已经完成派发。"
            team = session.get(Team, issue.initial_acceptance_team_id or issue.current_dev_team_id)
            team_name = team.name if team else "目标研发群"
            return "\n\n".join(
                (
                    "问题已派发",
                    f"目标研发群：**{_markdown_value(team_name)}**",
                    f"问题编号：**{_markdown_value(issue.case_ref)}**",
                    f"机器人引用编号：〔KF·{issue.case_ref}〕",
                )
            )

    def _proposal_reply(
        self,
        token: str,
        fields: Mapping[str, str],
        teams: Sequence[Mapping[str, str]],
        *,
        extra: str | None = None,
    ) -> str:
        team_by_id = {item["id"]: item["name"] for item in teams}
        target_id = fields.get("acceptance_team_id", "")
        target_name = team_by_id.get(target_id, "未确定")
        lines = [
            "问题受理提案（尚未派发）",
            "",
            f"问题标题：**{_markdown_value(fields.get('issue_title') or '未整理')}**",
            f"问题描述：**{_markdown_value(fields.get('issue_description') or '未补充')}**",
            "",
            f"目标研发群：**{_markdown_value(target_name)}**",
            f"问题模块：**{_markdown_value(fields.get('suspected_type') or '未确定')}**",
            f"现状说明：**{_markdown_value(fields.get('current_situation') or '未填写')}**",
        ]
        if fields.get("customer_name"):
            lines.append(f"客户 / 企业：**{_markdown_value(fields['customer_name'])}**")
        if fields.get("customer_contact_name"):
            lines.append(f"联系人：**{_markdown_value(fields['customer_contact_name'])}**")
        if fields.get("customer_contact_method"):
            lines.append(f"联系方式：**{_markdown_value(fields['customer_contact_method'])}**")
        missing = _missing_fields(fields, teams)
        if missing:
            lines.extend(("", "待补齐：" + "、".join(missing)))
        if "目标研发群" in missing:
            lines.append(
                "可选研发群："
                + (
                    "、".join(f"**{_markdown_value(item['name'])}**" for item in teams)
                    or "暂无可用群聊"
                )
            )
        if "问题模块" in missing:
            lines.append(
                "可选模块："
                + "、".join(f"**{_markdown_value(module.value)}**" for module in IssueModule)
            )
        if extra:
            lines.extend(("", extra))
        lines.extend(
            [
                "",
                f"会话标记：{INTAKE_MARKER_PREFIX}{token}〕",
                "",
                "请对照原始聊天核对问题描述是否完整准确；有遗漏或不准确时，引用本条提案并 @机器人 修改。"
                "资料核对无误且已补齐后，回复“确认派发”才会创建正式问题并推送到目标研发群。",
            ]
        )
        return "\n".join(lines)

    def _cleanup_expired(self, now: datetime) -> int:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("清理时间必须包含时区")
        with self._session_factory() as session:
            expired_ids = list(
                session.scalars(
                    select(GroupIssueIntakeSession.id).where(
                        GroupIssueIntakeSession.expires_at <= now
                    )
                ).all()
            )
        removed = 0
        for session_id in expired_ids:
            try:
                with self._session_factory() as session, session.begin():
                    record = session.get(GroupIssueIntakeSession, session_id)
                    if record is None or _as_utc(record.expires_at) > now:
                        continue
                    media_ids = {
                        UUID(item["media_id"])
                        for item in record.parts_json
                        if item.get("kind") == "image" and isinstance(item.get("media_id"), str)
                    }
                    for media_id in media_ids:
                        referenced = session.scalar(
                            select(CaseEntryPart.id)
                            .where(CaseEntryPart.media_id == media_id)
                            .limit(1)
                        )
                        if referenced is not None:
                            continue
                        media = session.get(StoredMedia, media_id)
                        if media is not None:
                            self._storage.delete_prefix(media.object_key)
                            session.delete(media)
                    turns = session.scalars(
                        select(GroupIssueIntakeTurn).where(
                            GroupIssueIntakeTurn.session_id == record.id
                        )
                    ).all()
                    for turn in turns:
                        session.delete(turn)
                    session.delete(record)
                removed += 1
            except Exception:
                logger.exception(
                    "expired_group_issue_intake_cleanup_failed session_id=%s", session_id
                )
        return removed

    def _delete_orphan_media(self, media_ids: Iterable[UUID]) -> None:
        with self._session_factory() as session, session.begin():
            for media_id in media_ids:
                referenced = session.scalar(
                    select(CaseEntryPart.id)
                    .where(CaseEntryPart.media_id == media_id)
                    .limit(1)
                )
                if referenced is not None:
                    continue
                media = session.get(StoredMedia, media_id)
                if media is None:
                    continue
                self._storage.delete_prefix(media.object_key)
                session.delete(media)


def _event_text(event: InboundEvent) -> str:
    return "".join(
        part.text if isinstance(part, InboundTextPart) else "[图片]"
        for part in event.parts
    ).strip()


def _markdown_value(value: str) -> str:
    """Escape untrusted Markdown punctuation while preserving value line breaks."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    escaped = [
        re.sub(r"([\\`*_{}\[\]()#+\-.!|>])", r"\\\1", line)
        for line in lines
    ]
    return "  \n".join(escaped)


def _validated_msgid(msgid: str) -> str:
    normalized = msgid.strip()
    if not normalized or len(normalized) > 256:
        raise ValueError("消息标识无效，无法安全处理。")
    return normalized


def _without_confirm(text: str) -> str:
    return text.replace(CONFIRM_PHRASE, "").strip(" \t\r\n，。！？.!?；;,:：")


def _missing_fields(
    fields: Mapping[str, str], teams: Sequence[Mapping[str, str]]
) -> list[str]:
    team_ids = {item["id"] for item in teams}
    missing: list[str] = []
    if not fields.get("issue_title", "").strip():
        missing.append("问题标题")
    if not fields.get("issue_description", "").strip():
        missing.append("问题描述")
    if fields.get("acceptance_team_id", "") not in team_ids:
        missing.append("目标研发群")
    if fields.get("suspected_type", "") not in {module.value for module in IssueModule}:
        missing.append("问题模块")
    return missing


def _normalize_fields(value: Mapping[str, Any]) -> dict[str, str]:
    return {
        key: value.get(key, "").strip() if isinstance(value.get(key, ""), str) else ""
        for key in _FIELD_KEYS
    }


def _part_from_json(value: Mapping[str, Any]) -> ContentPart:
    if value.get("kind") == PartKind.TEXT.value and isinstance(value.get("text"), str):
        return TextPart(value["text"])
    if value.get("kind") == PartKind.IMAGE.value and isinstance(value.get("media_id"), str):
        return ImagePart(UUID(value["media_id"]))
    raise ValueError("受理会话包含无效的原始消息内容")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
