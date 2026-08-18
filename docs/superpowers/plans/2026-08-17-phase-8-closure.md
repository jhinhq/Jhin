# Phase 8 Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the deterministic Phase 8 exit-test defect, prove all Phase 8 acceptance scenarios, and update the project status and architecture documentation.

**Architecture:** Keep the production approval contract unchanged: tools that do not advertise approval support must remain denied when policy requires approval. Correct the concurrency test fixture to use the existing inert approval-capable demonstration tool, then document the already-implemented Phase 8 delegation and team architecture with fresh verification evidence.

**Tech Stack:** Python 3.13, pytest/httpx, FastAPI, Temporal, PostgreSQL, Docker Compose, Markdown.

**Spec:** `docs/superpowers/specs/2026-08-17-jhin-ai-company-experience-design.md`

## Global Constraints

- Do not change `system.echo` to support approval; its denial is an intentional policy invariant.
- Preserve the five existing Phase 8 integration scenarios and their worker-restart coverage.
- PostgreSQL remains the source of truth; Temporal remains the durable workflow authority.
- Documentation must distinguish completed Phase 8 scope from later budget, connector, provider, and sandbox concurrency work.
- Do not claim completion until the focused Phase 8 suite and normal repository validation are freshly green.

---

### Task 1: Repair the Phase 8 concurrency acceptance fixture

**Files:**
- Modify: `tests/integration/test_phase8_exit.py:874`
- Reference: `packages/tools/src/jhin_tools/builtin.py:269`
- Reference: `tests/integration/test_phase4_exit.py:345`

**Interfaces:**
- Consumes: `system.demo.destructive`, an inert tool whose `ToolDefinition.supports_approval` is `True` and whose default destructive-risk policy requires approval.
- Produces: `test_concurrency_queues_second_task_and_survives_worker_restart`, which reliably parks task one before asserting task two queues and survives an agent-worker restart.

- [ ] **Step 1: Preserve the deterministic failing evidence**

Run:

```bash
uv run pytest -m integration tests/integration/test_phase8_exit.py::test_concurrency_queues_second_task_and_survives_worker_restart -v
```

Expected: FAIL at `assert approval_id` because `system.echo` returns `approval_unsupported`.

- [ ] **Step 2: Replace the unsupported tool fixture**

Change the agent grant and tool marker to the already-tested inert destructive tool; remove the explicit policy override because destructive risk already requires approval:

```python
agent = await _make_agent(
    client,
    ws,
    f"P8e worker {tag}",
    grants={"system.demo.destructive": {}},
)

first = await _assign(
    client,
    ws,
    agent["id"],
    f"P8e first {tag}",
    f'[[tool:system.demo.destructive {{"label": "hold-slot-{tag}"}}]]',
)
```

Delete the `PUT /policy` block that requires approval for `system.echo`.

- [ ] **Step 3: Run the isolated acceptance scenario**

Run:

```bash
uv run pytest -m integration tests/integration/test_phase8_exit.py::test_concurrency_queues_second_task_and_survives_worker_restart -v
```

Expected: PASS; task one reaches `waiting_approval`, task two records `agent_concurrency`, the worker restarts, and both tasks complete after approval.

- [ ] **Step 4: Run all five Phase 8 scenarios**

First run all five Phase 8 scenarios so documentation can be closed only from fresh evidence:

```bash
uv run pytest -m integration tests/integration/test_phase8_exit.py -v
```

Expected: 5 passed, including the worker-restart concurrency scenario.

- [ ] **Step 5: Run the focused policy regressions**

Run:

```bash
uv run pytest packages/policy/tests/test_evaluator.py packages/tools/tests/test_gateway.py -q
```

Expected: PASS, including the `approval_unsupported` invariant.

- [ ] **Step 6: Commit the fixture repair**

```bash
git add tests/integration/test_phase8_exit.py
git commit -m "test: repair Phase 8 concurrency approval fixture"
```

### Task 2: Draft the Phase 8 architecture reference

**Files:**
- Create: `docs/architecture/delegation-and-teams.md`

**Interfaces:**
- Consumes: the implemented `organization.delegate_task`, `organization.report_result`, `DelegatedTaskWorkflow`, `EngineeringTicketWorkflow`, task-tree API, structured message UI, and concurrency admission behavior.
- Produces: a durable Phase 8 architecture reference whose verification section remains explicitly pending until Task 3 passes every gate.

- [ ] **Step 1: Write the architecture document**

Create `docs/architecture/delegation-and-teams.md` with these concrete sections:

```markdown
# Delegation and Teams

## Scope
Phase 8 adds structured agent communication, authorized delegation, durable
child workflows, engineering/QA workflow templates, task lineage, and
workspace/agent concurrency admission.

## Message contract
Document instruction, question, status, result, delegation, review_request,
review_result, and escalation; explain artifacts, risks, and recommended next
action.

## Delegation authorization
Document deny-by-default grants, target relationship/pins, explicit deny,
cycle prevention, maximum task depth, and live gateway enforcement.

## Durable workflow flow
Document parent parking, DelegatedTaskWorkflow child execution, standardized
summary return, failure handling, and Temporal recovery.

## Engineering ticket template
Document direct/coordinator modes, implementation, optional manager review,
QA review, bounded failure/fix/retest, and final sync-back.

## Concurrency
Document agent/workspace admission, visible queued reason, slot wakeups, poll
fallback, and worker-restart behavior.

## API and UI
Document task tree, structured messages, queue banners, grant scopes,
concurrency settings, and template picker.

## Verification
List the five Phase 8 integration scenarios and state `Repository-wide
verification pending Task 3` without claiming unrun gates.

## Deferred scope
State that delegation budget enforcement and connector/provider/sandbox
concurrency belong to later phases.
```

- [ ] **Step 2: Commit the pending architecture reference**

```bash
git add docs/architecture/delegation-and-teams.md
git commit -m "docs: describe Phase 8 delegation architecture"
```

### Task 3: Run the Phase 8 and repository verification gates

**Files:**
- Modify: `docs/architecture/delegation-and-teams.md`
- Modify: `README.md:14`
- Modify: `README.md:128`
- Modify: `docs/implementation-plan.md:3257`
- Do not modify source unless a newly reproduced defect is diagnosed through the systematic debugging process.

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: fresh, recorded evidence that Phase 8 and the normal repository checks are green.

- [ ] **Step 1: Establish a reproducible integration environment**

Verify the master-key file/config exists without printing it, build the sandbox image, rebuild/start the Compose services from the current checkout, wait for health, and apply migrations:

```bash
docker compose --profile build build sandbox-image
docker compose build api agent-worker workflow-worker event-worker web
docker compose up -d
docker compose ps
docker compose exec -T api jhin-db-migrate
```

Expected: all required services healthy and migrations at head. Never print secret values.

- [ ] **Step 2: Run the complete focused Phase 8 integration suite**

```bash
uv run pytest -m integration tests/integration/test_phase8_exit.py -v
```

Expected: 5 passed.

- [ ] **Step 3: Run Python quality and unit gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not integration"
```

Expected: all commands pass.

- [ ] **Step 4: Run web gates**

```bash
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web test
pnpm --filter jhin-web exec next build --webpack
```

Expected: lint, TypeScript, Vitest, and the production webpack build pass.

- [ ] **Step 5: Run the complete integration suite**

```bash
uv run pytest -m integration tests/integration -v
```

Expected: all integration scenarios pass from the freshly rebuilt stack. Diagnose any state-order defect; do not omit a failing group from the recorded evidence.

- [ ] **Step 6: Close README, checklist, and verification evidence**

Under `## Verification`, replace the pending line with the date, commands, and counts from the fresh runs. Replace the README Phase 7 status with a Phase 8 summary covering durable SWE/QA handoff, direct/CTO coordination, structured messages, bounded retest, task lineage, and visible queuing; link the architecture document and add a short Delegation and teams walkthrough. Mark only the ten Phase 8 checklist items `[x]`, leave every other phase unchanged, and note that budget plus connector/provider/sandbox concurrency remain in later phases. Do not record an unexecuted command as passing.

- [ ] **Step 7: Verify documentation consistency**

```bash
rg -n "Status: Phase 7|Phase 8|delegation-and-teams|budget" README.md docs/architecture/delegation-and-teams.md docs/implementation-plan.md
git diff --check
```

Expected: no stale Phase 7 status, the architecture link resolves, exactly Phase 8 is checked, deferred scope is explicit, and no whitespace errors exist.

- [ ] **Step 8: Commit Phase 8 closure**

```bash
git add README.md docs/architecture/delegation-and-teams.md docs/implementation-plan.md
git commit -m "docs: close Phase 8 delegation and teams"
```
