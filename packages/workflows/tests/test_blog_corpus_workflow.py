from uuid import uuid4

from temporalio import activity
from temporalio.common import WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows.blog_corpus import BlogCorpusSyncInput, BlogCorpusSyncWorkflow
from jhin_workflows.blog_corpus.shared import (
    ACTIVITY_ADVANCE_BLOG_CORPUS_SYNC,
    BlogCorpusSyncResult,
    blog_corpus_workflow_id,
)
from jhin_workflows.task_queues import TOOL_TASK_QUEUE


class Pages:
    def __init__(self):
        self.pages = 0

    @activity.defn(name=ACTIVITY_ADVANCE_BLOG_CORPUS_SYNC)
    async def advance(self, params: BlogCorpusSyncInput) -> BlogCorpusSyncResult:
        self.pages += 1
        return BlogCorpusSyncResult(
            params.sync_id,
            "complete" if self.pages == 105 else "running",
            discovered=self.pages * 100,
            indexed=self.pages * 100,
        )


class Archive:
    """Stands in for the checkpointed sync row: the workflow carries no cursor."""

    # The real page size the tool worker requests, measured against Ghost's
    # 16 MiB response bound in jhin_connectors.ghost.archive.
    PAGE_SIZE = 20

    def __init__(self, total=4498, size=PAGE_SIZE, passes=3, unreadable=(7, 2500)):
        self.total, self.size, self.passes = total, size, passes
        self.unreadable = set(unreadable)
        self.pages, self.next_page, self.pass_number = [], 1, 1
        self.discovered = self.indexed = self.failed = 0
        self.failed_ids: list[str] = []
        self.status = "running"

    @activity.defn(name=ACTIVITY_ADVANCE_BLOG_CORPUS_SYNC)
    async def advance(self, params: BlogCorpusSyncInput) -> BlogCorpusSyncResult:
        last = -(-self.total // self.size)
        self.pages.append((self.pass_number, self.next_page))
        first = (self.next_page - 1) * self.size + 1
        for index in range(first, min(self.next_page * self.size, self.total) + 1):
            if self.pass_number == 1:
                self.discovered += 1
                if index in self.unreadable:
                    self.failed += 1
                    self.failed_ids.append(f"{index:024x}")
                else:
                    self.indexed += 1
        if self.next_page < last:
            self.next_page += 1
        elif self.pass_number < self.passes:
            self.pass_number, self.next_page = self.pass_number + 1, 1
        else:
            self.status = "partial"
        return BlogCorpusSyncResult(
            params.sync_id, self.status, self.discovered, self.indexed, self.failed
        )


async def test_corpus_sync_continues_history_and_preserves_persisted_progress():
    env = await WorkflowEnvironment.start_time_skipping()
    pages = Pages()
    try:
        async with Worker(
            env.client,
            task_queue=TOOL_TASK_QUEUE,
            workflows=[BlogCorpusSyncWorkflow],
            activities=[pages.advance],
        ):
            result = await env.client.execute_workflow(
                BlogCorpusSyncWorkflow.run,
                BlogCorpusSyncInput("workspace", str(uuid4())),
                id=f"corpus-test-{uuid4()}",
                task_queue=TOOL_TASK_QUEUE,
            )
            assert result.status == "complete"
            assert pages.pages == 105
            assert result.indexed == 10500
    finally:
        await env.shutdown()


async def test_dispatched_4498_post_sync_crosses_continue_as_new_with_its_cursor():
    env = await WorkflowEnvironment.start_time_skipping()
    archive = Archive()
    sync_id = str(uuid4())
    try:
        async with Worker(
            env.client,
            task_queue=TOOL_TASK_QUEUE,
            workflows=[BlogCorpusSyncWorkflow],
            activities=[archive.advance],
        ):
            # The recovery dispatcher starts the sync by name and id, never by handle.
            handle = await env.client.start_workflow(
                "BlogCorpusSyncWorkflow",
                BlogCorpusSyncInput("workspace", sync_id),
                id=blog_corpus_workflow_id(sync_id),
                task_queue=TOOL_TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                result_type=BlogCorpusSyncResult,
            )
            result: BlogCorpusSyncResult = await handle.result()
    finally:
        await env.shutdown()
    last = -(-4498 // Archive.PAGE_SIZE)
    assert last == 225
    # Three passes of 225 pages: six history boundaries, every cursor from the row.
    assert len(archive.pages) == 675 > 100
    assert archive.pages == [(number, page) for number in (1, 2, 3) for page in range(1, last + 1)]
    # What the workflow itself carries across continue-as-new is its identity and
    # the activity's own counts — never a cursor, and never a recomputed total.
    assert result.sync_id == sync_id
    assert result.status == "partial"
    assert (result.discovered, result.indexed, result.failed) == (4498, 4496, 2)
    assert result.indexed + result.failed == result.discovered
    assert len(archive.failed_ids) == result.failed
