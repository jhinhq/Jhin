from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from jhin_workflows.blog_corpus.shared import (
    ACTIVITY_ADVANCE_BLOG_CORPUS_SYNC,
    BlogCorpusSyncInput,
    BlogCorpusSyncResult,
)
from jhin_workflows.task_queues import TOOL_TASK_QUEUE


@workflow.defn(name="BlogCorpusSyncWorkflow")
class BlogCorpusSyncWorkflow:
    @workflow.run
    async def run(self, params: BlogCorpusSyncInput) -> BlogCorpusSyncResult:
        for _ in range(100):
            try:
                result: BlogCorpusSyncResult = await workflow.execute_activity(
                    ACTIVITY_ADVANCE_BLOG_CORPUS_SYNC,
                    params,
                    task_queue=TOOL_TASK_QUEUE,
                    result_type=BlogCorpusSyncResult,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(
                        maximum_attempts=5,
                        initial_interval=timedelta(seconds=2),
                        maximum_interval=timedelta(seconds=30),
                    ),
                )
            except Exception:
                failed: BlogCorpusSyncResult = await workflow.execute_activity(
                    ACTIVITY_ADVANCE_BLOG_CORPUS_SYNC,
                    BlogCorpusSyncInput(params.workspace_id, params.sync_id, fail=True),
                    task_queue=TOOL_TASK_QUEUE,
                    result_type=BlogCorpusSyncResult,
                    start_to_close_timeout=timedelta(seconds=30),
                )
                return failed
            if result.status not in {"queued", "running"}:
                return result
        workflow.continue_as_new(params)
