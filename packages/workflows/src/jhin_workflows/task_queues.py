"""Named Temporal task queues.

Agent runs get their own queue and worker (plan architecture): the agent
worker holds model/secret dependencies and can scale independently of the
general workflow worker.
"""

WORKFLOW_TASK_QUEUE = "jhin-workflow-queue"
AGENT_TASK_QUEUE = "jhin-agent-queue"
TOOL_TASK_QUEUE = "jhin-tool-queue"
