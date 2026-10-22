"""Background delivery of durable issue-status evaluations to the Agent."""

from __future__ import annotations

import asyncio
import logging

from kefu.agent.service import Agent
from kefu.case_desk.contracts import IssueStatusUpdateWork
from kefu.case_desk.service import CaseDesk

logger = logging.getLogger(__name__)


class IssueStatusUpdateWorker:
    def __init__(self, desk: CaseDesk, agent: Agent, *, batch_size: int = 5) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be greater than zero")
        self._desk = desk
        self._agent = agent
        self._batch_size = batch_size

    async def process_pending(self) -> int:
        work_items = await asyncio.to_thread(
            self._desk.claim_pending_issue_status_updates,
            self._batch_size,
        )
        results = await asyncio.gather(*(self._process_work(work) for work in work_items))
        return sum(results)

    async def _process_work(self, work: IssueStatusUpdateWork) -> int:
        # Work items are immutable CaseDesk contracts; keeping this method
        # independent lets the HTTP calls run concurrently without extending
        # the lease of jobs that wait behind a slow Agent response.
        request_id = f"issue-status-{work.id.hex}"
        try:
            status = await self._agent.classify_issue_status(work, request_id=request_id)
            completed = await asyncio.to_thread(
                self._desk.complete_issue_status_update,
                work.claims,
                status,
            )
            return completed
        except Exception as error:
            logger.exception(
                "issue_status_agent_update_failed job_id=%s issue=%s entry_sequence=%s",
                work.id,
                work.case_ref,
                work.entry_sequence,
            )
            try:
                await asyncio.to_thread(
                    self._desk.fail_issue_status_update,
                    work.claims,
                    str(error),
                )
            except Exception:
                logger.exception("issue_status_retry_persistence_failed job_id=%s", work.id)
            return 0
