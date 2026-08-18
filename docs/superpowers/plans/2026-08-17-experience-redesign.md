# Chat-First Experience Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Jhin's dense operations console with a friendly, beautiful, responsive AI-company experience centered on chats, agents, and optional teams, while preserving every technical control under Advanced.

**Architecture:** Keep FastAPI, same-origin cookies/CSRF, and TanStack Query as the single frontend data path. Thin Next.js App Router pages compose typed client features. The shell exposes five plain-language destinations and one Advanced disclosure. Human-readable activity cards are shared across chats, agent profiles, teams, and attention; sanitized raw evidence remains available only in Advanced.

**Tech Stack:** Next.js 16 App Router, React 19, TypeScript 5, Tailwind CSS 4, TanStack Query, Lucide, Vitest/Testing Library, Playwright, axe-core, FastAPI APIs from Releases 1–3.

**Spec:** `docs/superpowers/specs/2026-08-17-jhin-ai-company-experience-design.md`

## Global Constraints

- Use the supplied `/Users/varand/Downloads/noun-j-shape-853331.svg` as the brand mark without changing its geometry or aspect ratio.
- Desktop/tablet primary navigation is Chats, Agents, Company, Automations, and Needs your attention; mobile is Chats, Agents, Company, and More.
- Apps, Members, and Settings live in the workspace menu; operations live under one Advanced disclosure.
- Default screens contain no raw event names, workflow IDs, capability strings, or unsanitized tool payloads.
- Safety-critical state, consequences, pause/stop, failures, approvals, and reviews stay visible in plain language.
- Every legacy operational capability remains reachable under Advanced and every old URL has a compatibility redirect.
- Preserve the existing FastAPI/TanStack Query cookie-and-CSRF model; do not add duplicate Next route handlers or Server Actions.
- Next 16 dynamic pages await `params`; client pathname/search-param consumers have Suspense boundaries.
- Target WCAG 2.2 AA, 44px minimum targets, keyboard/touch parity, visible focus, semantic labels, reduced motion, and text-plus-color status.
- Wide layouts use three panes, medium two panes, and small screens one pane with bottom navigation and safe-area-aware composer.
- Authenticated avatars use explicit dimensions/sizes and `unoptimized` so workspace cookies reach media endpoints.
- Every create/edit/decision surface renders viewer/member/admin capabilities from workspace RBAC and never relies on a server rejection as its only access explanation.
- Every degraded-state message uses one contract: what failed, whether Jhin will retry, what is affected, and the safest next action.
- The Release 4 schema head is migration `0020`; every migration acceptance run includes `tests/integration/test_release1_migrations.py` and proves the complete `0017 -> 0018 -> 0019 -> 0020` upgrade/downgrade chain.

---

### Task 1: Establish typed product contracts, routes, and presentation adapters

**Files:**
- Modify: `apps/web/lib/types.ts`
- Create: `apps/web/lib/routes.ts`
- Create: `apps/web/lib/activity-presentation.ts`
- Create: `apps/web/lib/failure-presentation.ts`
- Create: `apps/web/lib/queries/conversations.ts`
- Create: `apps/web/lib/queries/agents.ts`
- Create: `apps/web/lib/queries/company.ts`
- Create: `apps/web/lib/queries/attention.ts`
- Create: `apps/web/tests/routes.test.ts`
- Create: `apps/web/tests/activity-presentation.test.ts`
- Create: `apps/web/tests/failure-presentation.test.ts`

**Interfaces:**
- `ConversationSummary {id,title,status,primary_agent,pinned_at,archived_at,last_activity_at,last_message_preview}`; `ConversationDetail` adds `participants`; `ConversationMessage {id,sequence,sender,recipient,message_type,content,created_at,task_id}`.
- `ChatTimelineEntry = {kind:"message", message} | {kind:"activity", card} | {kind:"attention", item}` and the server-provided `ActivityCardModel` union; client code styles its `kind`, `label`, `summary`, `status`, `participants`, `occurred_at`, and links but never derives labels from raw events.
- `AttentionItem` mirrors Release 3 approval/review/blocked/request union and counts/version response.
- `AgentDirectoryEntry`, `MediaAsset`, and `AvatarRef` mirror API DTOs. `AgentProfile` and `CompanyOverview` are explicit client composition models produced by adapters from parallel API queries, not invented transport fields.
- Central route helpers for simple and Advanced destinations.
- Query keys: `workspace/{id}/conversations/{filters}`, `conversation/{id}`, `conversation/{id}/messages`, `agents/{filters}`, `agent/{id}`, `company/{view}`, `attention/{filters}`, and `activity/{scope}` represented as stable tuple factories.
- `FailurePresentation {title, failed, retry, affected, nextAction, retryable}`.

- [ ] **Step 1: Write failing contract/presentation tests**

Test route construction/encoding, server-label preservation, status tones, friendly fallback text, default redaction of raw identifiers/capabilities, an explicit Advanced evidence adapter, failure four-part copy, and exhaustive union handling.

```bash
pnpm --filter jhin-web test -- routes.test.ts activity-presentation.test.ts failure-presentation.test.ts
```

Expected: FAIL because adapters and route helpers are absent.

- [ ] **Step 2: Add API-matching types and route vocabulary**

Mirror exact Release 1–3 endpoints: `/conversations`, `/conversations/{id}`, `/conversations/{id}/messages`, `/conversations/{id}/turns`, `/directory`, `/agents/{id}`, agent memberships/relationships/avatar, `/memory`, `/activity`, `/attention`, approvals, reviews, teams, and org graph. Use discriminated unions with exhaustive `never` checks. Composite view adapters own parallel loading/error states. Route helpers replace literal internal links across later tasks.

- [ ] **Step 3: Implement friendly presentation adapters**

Preserve Release 3's normalized card kind/label/redaction and add only icon, tone, action label, and layout. Produce consequence text while keeping raw evidence separate. Map API error codes and degraded states into the four-part failure contract, including memory retry/unavailable/full-text/contested, revocation, missing reviewer, and provider/tool failures.

- [ ] **Step 4: Add TanStack Query modules**

Refactor Release 2 conversation hooks from `lib/hooks.ts` into the single query-key namespace; do not leave duplicate queries. Wrap existing `api.ts` fetch/CSRF conventions. Poll active chats/attention only. Invalidation matrix: turn/message → conversation detail/messages/list/activity/attention; agent/membership/relationship/avatar/memory → agent/directory/company/activity; approval/review decision → attention/activity/source conversation; App/Automation mutation → corresponding simple and Advanced queries. No duplicate transport layer.

- [ ] **Step 5: Run tests/typecheck and commit**

```bash
pnpm --filter jhin-web test -- routes.test.ts activity-presentation.test.ts failure-presentation.test.ts
pnpm --filter jhin-web typecheck
git add apps/web/lib apps/web/tests/routes.test.ts apps/web/tests/activity-presentation.test.ts apps/web/tests/failure-presentation.test.ts
git commit -m "refactor: define friendly product presentation contracts"
```

### Task 2: Install the Jhin brand, light-first tokens, and warm-dark theme

**Files:**
- Create: `apps/web/public/brand/jhin-mark.svg`
- Create: `apps/web/public/brand/ATTRIBUTION.md`
- Create: `apps/web/app/icon.svg`
- Delete: `apps/web/app/favicon.ico`
- Create: `apps/web/components/brand/jhin-mark.tsx`
- Create: `apps/web/components/brand/brand-lockup.tsx`
- Create: `apps/web/components/theme/theme-provider.tsx`
- Create: `apps/web/components/theme/theme-toggle.tsx`
- Create: `apps/web/lib/theme.ts`
- Create: `apps/web/lib/contrast.ts`
- Modify: `apps/web/app/layout.tsx`
- Modify: `apps/web/app/globals.css`
- Modify: `apps/web/components/providers.tsx`
- Modify: `apps/web/components/auth-card.tsx`
- Create: `apps/web/tests/brand.test.tsx`
- Create: `apps/web/tests/theme.test.tsx`
- Modify: `apps/web/package.json`
- Modify: `pnpm-lock.yaml`

**Interfaces:**
- `Theme = "light" | "dark" | "system"` persisted through a same-site cookie.
- `JhinMark` uses `currentColor`; `BrandLockup` combines the mark and accessible wordmark.
- Root theme is derived server-side from the cookie to prevent flash.
- Manrope is bundled locally from pinned `@fontsource-variable/manrope@5.2.8` (OFL-1.1 license shipped by the package); no runtime font request is permitted.

- [ ] **Step 1: Write failing brand/theme tests**

Test accessible mark title/hidden modes, exact source viewBox/path hash, Iris current-color treatment, light default, system preference, cookie persistence, no hydration mismatch, no stale favicon, no external font request, and WCAG contrast ratios for both palettes using `contrastRatio(foreground, background)`.

```bash
pnpm --filter jhin-web test -- brand.test.tsx theme.test.tsx
```

Expected: FAIL because brand/theme modules do not exist.

- [ ] **Step 2: Copy and normalize the supplied mark**

Copy only the SVG geometry/viewBox into the repository asset, remove fixed black/white presentation, and use `fill="currentColor"`. Create the app icon from the same path in an Iris square field, remove the old favicon, and do not trace or redraw it. Record that the product owner supplied `noun-j-shape-853331.svg`, its SHA-256, source filename/date, and that external distribution rights/attribution remain the product owner's responsibility; do not invent an author or license.

- [ ] **Step 3: Self-host Manrope and define design tokens**

Install the pinned Fontsource package and import its variable Latin CSS from the root layout so Next bundles WOFF2 files locally; retain system fallbacks. Light tokens are the approved spec values. Warm-dark tokens are deterministic: canvas `#171713`, surface `#20211C`, raised `#292A24`, ink `#F3F2EC`, muted `#B8B9B0`, border `#3A3B34`, Iris `#A9A4FF`/tint `#2B2859`, sage `#74C99F`/`#173C2E`, amber `#E7B35E`/`#493416`, coral `#F08B94`/`#4C2229`, sky `#7FC2E6`/`#173747`, peach `#E79A73`/`#4B2B1F`. Define spacing, radii, shadows, focus ring, content width, and motion tokens; test all text/background pairs at 4.5:1 (3:1 for large text/UI boundaries).

- [ ] **Step 4: Implement no-flash theme control**

Make root layout async, read the cookie, set `data-theme`, and provide a client toggle for light/dark/system. Honor `prefers-color-scheme` and `prefers-reduced-motion`.

- [ ] **Step 5: Update auth/setup brand treatment**

Replace the temporary letter J with the real lockup and friendly surface styles without changing authentication behavior.

- [ ] **Step 6: Run visual foundation gates and commit**

```bash
pnpm --filter jhin-web test -- brand.test.tsx theme.test.tsx
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
git add apps/web/public/brand apps/web/app/icon.svg apps/web/app/favicon.ico apps/web/components/brand apps/web/components/theme apps/web/lib/theme.ts apps/web/lib/contrast.ts apps/web/app/layout.tsx apps/web/app/globals.css apps/web/components/providers.tsx apps/web/components/auth-card.tsx apps/web/tests/brand.test.tsx apps/web/tests/theme.test.tsx apps/web/package.json pnpm-lock.yaml
git commit -m "feat: introduce the Jhin visual system"
```

### Task 3: Build accessible interaction primitives

**Files:**
- Modify: `apps/web/components/ui.tsx`
- Create: `apps/web/components/ui/avatar.tsx`
- Create: `apps/web/components/ui/drawer.tsx`
- Create: `apps/web/components/ui/status-label.tsx`
- Create: `apps/web/components/ui/tabs.tsx`
- Create: `apps/web/components/ui/failure-state.tsx`
- Create: `apps/web/components/accessibility/skip-link.tsx`
- Create: `apps/web/components/accessibility/live-region.tsx`
- Create: `apps/web/tests/setup.ts`
- Create: `apps/web/tests/ui-accessibility.test.tsx`
- Modify: `apps/web/vitest.config.ts`
- Modify: `apps/web/package.json`
- Modify: `pnpm-lock.yaml`

**Interfaces:**
- Buttons/fields/links meet 44px minimum target by default.
- Native-dialog-backed `Drawer` traps focus, closes on Escape, and restores trigger focus.
- `StatusLabel` always includes visible text.
- `Tabs` supports arrow/Home/End and correct ARIA relationships.
- `FailureState` renders all four `FailurePresentation` fields, optional retry action, and polite/urgent announcement by consequence.

- [ ] **Step 1: Write failing keyboard/ARIA tests**

Install `@testing-library/user-event` in this task. Add deterministic jsdom `HTMLDialogElement.showModal/close` behavior in the shared test setup. Use `user-event` to cover button targets, focus-visible classes, dialog open/close/focus restoration, tab keyboard behavior, avatar fallbacks/names, skip link, live region, disabled/loading semantics, and reduced-motion classes.

Configure Vitest's shared setup in the same red step so every component suite executes it:

```ts
// apps/web/vitest.config.ts
test: {
  environment: "jsdom",
  include: ["tests/**/*.test.{ts,tsx}"],
  setupFiles: ["./tests/setup.ts"],
}
```

```bash
pnpm --filter jhin-web test -- ui-accessibility.test.tsx
```

Expected: FAIL because primitives are missing or undersized.

- [ ] **Step 2: Refactor shared primitives**

Preserve current component exports while applying new tokens and targets. Prefer native semantic elements. Require accessible names at the type/API boundary where practical.

- [ ] **Step 3: Add dialog, tabs, avatar, and status components**

Avoid custom focus-trap libraries when native `<dialog>` plus tested focus restoration suffices. Avatar always has adjacent visible name in product compositions; fallback initials are decorative when name text is present.

- [ ] **Step 4: Run component and existing UI regressions**

```bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
```

Expected: PASS.

- [ ] **Step 5: Commit primitives**

```bash
git add apps/web/components/ui.tsx apps/web/components/ui apps/web/components/accessibility apps/web/tests/setup.ts apps/web/tests/ui-accessibility.test.tsx apps/web/vitest.config.ts apps/web/package.json pnpm-lock.yaml
git commit -m "feat: add accessible interface primitives"
```

### Task 4: Replace the operations sidebar with a responsive chat-first shell

**Files:**
- Modify: `apps/web/components/app-shell.tsx`
- Modify: `apps/web/app/(app)/layout.tsx`
- Create: `apps/web/components/shell/primary-nav.tsx`
- Create: `apps/web/components/shell/chat-rail.tsx`
- Create: `apps/web/components/shell/mobile-nav.tsx`
- Create: `apps/web/components/shell/advanced-nav.tsx`
- Create: `apps/web/components/shell/workspace-menu.tsx`
- Create: `apps/web/components/shell/page-header.tsx`
- Create: `apps/web/components/advanced/system-health-panel.tsx`
- Create: `apps/web/app/(app)/advanced/system/page.tsx`
- Create (temporary compatibility redirect): `apps/web/app/(app)/agents/page.tsx`
- Create (temporary compatibility redirect): `apps/web/app/(app)/company/page.tsx`
- Create (temporary compatibility redirect): `apps/web/app/(app)/automations/page.tsx`
- Create (temporary compatibility redirect): `apps/web/app/(app)/attention/page.tsx`
- Create (temporary compatibility redirect): `apps/web/app/(app)/apps/page.tsx`
- Create (temporary compatibility redirect): `apps/web/app/(app)/members/page.tsx`
- Create: `apps/web/lib/navigation.ts`
- Create: `apps/web/tests/navigation.test.ts`
- Create: `apps/web/tests/app-shell.test.tsx`

**Interfaces:**
- Desktop/tablet primary: Chats, Agents, Company, Automations, Needs your attention.
- Mobile primary: Chats, Agents, Company, More; More opens Automations, Needs your attention, workspace menu, and Advanced.
- Workspace menu: Apps, Members, Settings.
- One collapsed-by-default Advanced group.
- Desktop rail/content/context, tablet rail/content, mobile one-pane/bottom-nav layouts.

- [ ] **Step 1: Write failing navigation/shell and temporary-route tests**

Assert exact desktop/mobile information architecture, active route matching, Needs your attention count label, Advanced state, workspace/More menu keyboard behavior, skip-to-content, semantic landmarks/classes, safe-area variables, PageHeader compatibility re-export, early System route, and no operational entries in primary nav. Also assert that every new primary/workspace-menu URL is navigable before its friendly screen ships: `/agents` → `/organization`, `/company` → `/organization`, `/automations` → `/triggers`, `/attention` → `/approvals`, `/apps` → `/connectors`, and `/members` → `/settings`. These are server redirects to existing functional surfaces, preserve a no-404 cutover, and are replaced by the named route pages in Tasks 6–9. Defer actual viewport composition assertions to Playwright because jsdom does not evaluate container/media queries.

```bash
pnpm --filter jhin-web test -- navigation.test.ts app-shell.test.tsx
```

Expected: FAIL against the old ten-item sidebar.

- [ ] **Step 2: Define navigation as data**

Use route helpers and semantic groups. Persist Advanced disclosure per browser without hiding required attention state. Icons accompany, never replace, labels. Mobile exposes exactly four bottom items and maintains the full desktop destinations through More.

- [ ] **Step 3: Implement responsive shell**

Use CSS grid/media/container queries and drawers rather than JavaScript viewport branching. Main content receives stable landmark/focus target. Mobile bottom navigation reserves composer/safe-area space.

- [ ] **Step 4: Integrate the shell without route cutover yet**

Existing pages render in the new content frame until migrated. Operational routes appear only inside Advanced navigation. Add the six temporary server redirect pages named in this task; do not point primary navigation at an unimplemented route or add a client-effect redirect. Task 6 replaces `/attention`, Task 7 replaces `/agents`, Task 8 replaces `/company`, and Task 9 replaces `/apps`, `/automations`, and `/members` atomically with their friendly implementations.

- [ ] **Step 5: Preserve PageHeader and System before root cutover**

Move the current root System/health card into `SystemHealthPanel`, mount it both on the current root and `/advanced/system`, and keep `PageHeader` re-exported from `components/app-shell.tsx` while old pages still import it. This prevents Task 5 from displacing health access and prevents intermediate build breakage.

- [ ] **Step 6: Run shell/frontend gates and commit**

```bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
git add apps/web/components/app-shell.tsx apps/web/app/'(app)'/layout.tsx apps/web/app/'(app)'/advanced/system apps/web/app/'(app)'/agents/page.tsx apps/web/app/'(app)'/company/page.tsx apps/web/app/'(app)'/automations/page.tsx apps/web/app/'(app)'/attention/page.tsx apps/web/app/'(app)'/apps/page.tsx apps/web/app/'(app)'/members/page.tsx apps/web/components/shell apps/web/components/advanced/system-health-panel.tsx apps/web/lib/navigation.ts apps/web/tests/navigation.test.ts apps/web/tests/app-shell.test.tsx
git commit -m "feat: add responsive chat-first navigation"
```

### Task 5: Build the complete named-chat workspace

**Files:**
- Create: `apps/web/app/(app)/chats/layout.tsx`
- Modify: `apps/web/app/(app)/chats/page.tsx`
- Create: `apps/web/app/(app)/chats/error.tsx`
- Create: `apps/web/app/(app)/chats/new/page.tsx`
- Modify: `apps/web/app/(app)/chats/[conversationId]/page.tsx`
- Create: `apps/web/app/(app)/chats/[conversationId]/error.tsx`
- Modify: `apps/web/app/(app)/page.tsx`
- Create: `apps/web/components/chats/chat-workspace.tsx`
- Create: `apps/web/components/chats/chat-list.tsx`
- Create: `apps/web/components/chats/chat-header.tsx`
- Create: `apps/web/components/chats/new-chat-flow.tsx`
- Create: `apps/web/components/chats/chat-composer.tsx`
- Create: `apps/web/components/chats/chat-transcript.tsx`
- Create: `apps/web/components/chats/message-row.tsx`
- Create: `apps/web/components/chats/activity-card.tsx`
- Create: `apps/web/components/chats/handoff-card.tsx`
- Create: `apps/web/components/chats/artifact-card.tsx`
- Create: `apps/web/components/chats/conversation-context.tsx`
- Create: `apps/web/components/chats/new-updates-button.tsx`
- Delete after migration: `apps/web/components/chat/chat-list.tsx`
- Delete after migration: `apps/web/components/chat/conversation-view.tsx`
- Delete after migration: `apps/web/components/chat/composer.tsx`
- Delete after migration: `apps/web/components/chat/message-row.tsx`
- Create: `apps/web/lib/chat-timeline.ts`
- Create: `apps/web/tests/chat-timeline.test.ts`
- Create: `apps/web/tests/chat-transcript.test.tsx`
- Create: `apps/web/tests/new-chat-flow.test.tsx`
- Delete after replacement: `apps/web/tests/conversation-view.test.tsx`

**Interfaces:**
- Opening Jhin restores the last chat or shows a large new-chat composer with suggested agents.
- Transcript interleaves messages, human-readable work activity, handoffs, artifacts, reviews, and approvals.
- Composer stays available; active work can be paused/stopped; context is a side panel/drawer.
- Last-chat key is `jhin:last-conversation:{workspaceId}` in local storage, written only after an accessible detail loads; missing/archived/404 values are cleared and fall back to the most recent accessible conversation, then `/chats`.

- [ ] **Step 1: Write failing timeline/chat behavior tests**

Cover stable merged ordering/dedupe, normal user/agent messages, visible agent-agent discussion summaries, handoffs/artifacts, inline attention, composer submission/idempotent retry, server-suggested editable title, rename/pin/archive, context drawer, task detail only on expansion, local last-chat restore and invalid fallback, query loading/404/error/reconnect states, and source links. `error.tsx` is explicitly a client error boundary accepting `{error, reset}`.

```bash
pnpm --filter jhin-web test -- chat-timeline.test.ts chat-transcript.test.tsx new-chat-flow.test.tsx
```

Expected: FAIL because the redesigned chat workspace is absent.

- [ ] **Step 2: Build query-backed chat rail and new-chat flow**

Refactor the Release 2 pages and `components/chat/*` into the new workspace; do not create a parallel implementation. Search/group pinned and recent chats, display agent names/avatars and plain statuses, and choose one primary agent for a new chat. Consume the Release 2 deterministic suggested title and keep it editable.

- [ ] **Step 3: Build transcript and conversational work cards**

Keep visible messages visually primary. Activity cards are compact and expandable; agent-to-agent cards name both agents and show request/result summaries. Never label internal summaries as hidden reasoning. Use only Task 1 query keys and delete the Release 2 duplicate hook/component/test implementations after consumers move.

- [ ] **Step 4: Implement scroll and live-state behavior**

Do not force scroll while the reader is above the latest content. Announce new updates in a polite live region and show a New updates button. Keep composer above mobile keyboard/safe areas.

- [ ] **Step 5: Route root and agent actions into chats**

Root restores the last accessible conversation; otherwise `/chats`. Chat links become the primary agent action. Keep compatibility route behavior for bookmarks.

- [ ] **Step 6: Handle client-fetch route states correctly**

Because TanStack Query owns data fetching, render loading skeletons and API 404/not-found state inside `ChatWorkspace`; do not rely on App Router `loading.tsx`/`not-found.tsx` for client responses. Dynamic pages await `params`; client navigation/search hooks stay below Suspense.

- [ ] **Step 7: Run chat/frontend gates and commit**

```bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
git add apps/web/app/'(app)'/chats apps/web/app/'(app)'/page.tsx apps/web/components/chats apps/web/components/chat/chat-list.tsx apps/web/components/chat/conversation-view.tsx apps/web/components/chat/composer.tsx apps/web/components/chat/message-row.tsx apps/web/lib/chat-timeline.ts apps/web/tests/chat-timeline.test.ts apps/web/tests/chat-transcript.test.tsx apps/web/tests/new-chat-flow.test.tsx apps/web/tests/conversation-view.test.tsx
git commit -m "feat: redesign work around named chats"
```

### Task 6: Consolidate approvals and reviews into Needs your attention

**Files:**
- Replace temporary compatibility redirect: `apps/web/app/(app)/attention/page.tsx`
- Create: `apps/web/components/attention/attention-list.tsx`
- Create: `apps/web/components/attention/attention-card.tsx`
- Create: `apps/web/components/attention/approval-decision-card.tsx`
- Create: `apps/web/components/attention/review-decision-card.tsx`
- Create: `apps/web/components/advanced/approval-evidence-card.tsx`
- Create: `apps/web/app/(app)/advanced/attention/page.tsx`
- Modify then delete: `apps/web/components/approval-card.tsx`
- Replace: `apps/web/app/(app)/approvals/page.tsx`
- Create: `apps/web/tests/attention-card.test.tsx`
- Create: `apps/web/tests/advanced-approval-card.test.tsx`
- Modify then delete: `apps/web/tests/approval-card.test.tsx`

**Interfaces:**
- One inbox for approvals, work reviews, failed/blocked work, and requested decisions.
- Default card answers who/what/why/consequence/next action.
- Advanced evidence preserves current capability/action/payload inspection.
- Inbox reads the Release 3 `/attention` union/count/version endpoint; decisions call existing `/approvals/{id}/approve|reject` or `/reviews/{id}/decision` and invalidate attention, activity, source chat, and Advanced evidence.

- [ ] **Step 1: Write failing decision-card tests**

Test plain-language approval/review/blocked/requested-decision states, source chat link from the backend projection, consequence text, viewer read/member-assigned/admin action gating, keyboard decisions, loading/double-submit prevention, resolved state, count/version invalidation, inline chat variant, and absence of raw capability/action strings in default markup.

```bash
pnpm --filter jhin-web test -- attention-card.test.tsx advanced-approval-card.test.tsx
```

Expected: FAIL because the unified attention UI is absent.

- [ ] **Step 2: Build inbox and decision cards**

Group urgent/soon/resolved from the unified server cursor, show agent/avatar/source chat, and use the exact invalidation matrix after decisions. Explicitly distinguish AI work review from human security approval. Do not issue one query per item to discover its conversation.

- [ ] **Step 3: Preserve raw evidence under Advanced**

Move/refactor the existing approval card and its tests into the Advanced evidence component, create `/advanced/attention` in this task, update imports, then delete the old component/test. Add a visible `View technical details` link from the default card; no intermediate commit contains a broken link.

- [ ] **Step 4: Redirect the legacy approval route**

Use an App Router server redirect from `/approvals` to `/attention`; do not duplicate inbox logic.

- [ ] **Step 5: Run attention gates and commit**

```bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
git add apps/web/app/'(app)'/attention apps/web/app/'(app)'/approvals apps/web/app/'(app)'/advanced/attention apps/web/components/attention apps/web/components/advanced apps/web/components/approval-card.tsx apps/web/tests/attention-card.test.tsx apps/web/tests/advanced-approval-card.test.tsx apps/web/tests/approval-card.test.tsx
git commit -m "feat: unify decisions in a friendly attention inbox"
```

### Task 7: Redesign the agent directory, profiles, builder, avatars, and memory

**Files:**
- Replace temporary compatibility redirect: `apps/web/app/(app)/agents/page.tsx`
- Create: `apps/web/app/(app)/agents/[agentId]/page.tsx`
- Create: `apps/web/app/(app)/agents/[agentId]/edit/page.tsx`
- Modify: `apps/web/app/(app)/agents/new/page.tsx`
- Modify: `apps/web/lib/wizard.ts`
- Modify: `apps/web/lib/api.ts`
- Create: `apps/web/lib/agent-profile.ts`
- Create: `apps/web/components/agents/agent-card.tsx`
- Create: `apps/web/components/agents/agent-directory.tsx`
- Create: `apps/web/components/agents/agent-profile.tsx`
- Create: `apps/web/components/agents/agent-builder.tsx`
- Create: `apps/web/components/agents/agent-advanced-panel.tsx`
- Create: `apps/web/components/agents/memory-panel.tsx`
- Create: `apps/web/components/avatar/agent-avatar.tsx`
- Create: `apps/web/components/avatar/avatar-editor.tsx`
- Create: `apps/web/components/avatar/avatar-upload.tsx`
- Create: `apps/web/components/avatar/avatar-generation.tsx`
- Create: `apps/web/components/avatar/crop-avatar.ts`
- Modify: `apps/web/components/org/agent-drawer.tsx`
- Create: `apps/web/tests/agent-card.test.tsx`
- Create: `apps/web/tests/agent-builder.test.tsx`
- Create: `apps/web/tests/avatar.test.tsx`
- Create: `apps/web/tests/memory-panel.test.tsx`
- Create: `apps/web/tests/api-multipart.test.ts`
- Modify: `apps/web/tests/wizard.test.ts`

**Interfaces:**
- Search/filter by name, role, expertise, team, availability; Chat is primary action.
- Profile: identity, purpose, expertise, apps, teams, collaborators, current work/activity, memory controls.
- Builder: Describe help → Personalize identity/avatar → Apps/review style/optional team → Create and chat.
- Discoverability and availability are explicit Release 1-backed fields, not inferred from team membership or a paused/active status: `discoverability` controls directory/new-chat suggestion eligibility and `availability` supplies the visible routing state. Only owner/admin can set either field on create/edit; viewer/member see the current values and a plain-language explanation that an admin controls them.
- Advanced: system prompt, model, budget, limits, detailed grants.
- `buildAgentProfile` composes agent detail, memberships, relationships, directory identity, grants/policy summary, visible memories, and agent-filtered activity from parallel Release 1–3 queries.
- `apiFormData(path, formData, options)` sends cookie credentials and CSRF header but lets the browser set the multipart boundary; it never JSON-stringifies `FormData`.

- [ ] **Step 1: Write failing directory/builder/avatar/memory tests**

Cover teamless/managerless agents, multi-team badges, public identity, close collaborator display, search, Chat action, viewer/member/admin gating, pause/resume/delete with consequences, exact profile query composition, three-step progressive form, review-style preset mapping, explicit discoverability toggle and availability selector persistence, directory/new-chat inclusion only for active discoverable available agents, a visible unavailable/not-discoverable explanation, and owner/admin-only edit controls with viewer/member read-only explanations. Also cover staged partial-failure retry without duplicate agent, initials/upload/generation, canvas crop output, multipart boundary/CSRF, generation disclosure/failure fallback, authenticated image sizing, memory pin/edit/contest/promotion/share/forget, and Advanced-only raw settings.

```bash
pnpm --filter jhin-web test -- agent-card.test.tsx agent-builder.test.tsx avatar.test.tsx memory-panel.test.tsx wizard.test.ts
```

Expected: FAIL against the settings-heavy drawer/wizard.

- [ ] **Step 2: Build agent directory and full profiles**

Use the explicit `buildAgentProfile` parallel queries, visible names beside every avatar, availability text, optional team/manager sections, and source-linked current work. Make Chat dominant. Preserve pause/resume/delete for authorized users with plain-language effects and confirmation; viewer sees state without mutation controls.

- [ ] **Step 3: Refactor the agent builder**

Split the current monolithic page into focused steps. Default access comes from selected Apps and one server preset (`hands_off`, `exceptions`, `manager_before_close`, `always_before_close`); teams/managers remain optional; prompt/model/budget/limits are Advanced. In the Personalize step, expose the existing Release 1 `discoverability` and `availability` fields with short consequences: discoverable agents appear in directory and suggested-chat results; unavailable agents remain visible but cannot be selected for new work. Send their exact enum values in the existing create/update DTO, never a UI-only flag. Owner/admin may edit them; member/viewer routes render read-only current values and no mutation request. Submit as a staged saga: create the core agent paused once, persist its ID, apply memberships/grants/review preset, then activate; a required-step failure leaves one paused `Setup incomplete` agent with Retry/Edit/Delete, never a duplicate. Avatar generation is nonblocking. First-chat failure keeps the active agent and offers Retry chat.

- [ ] **Step 4: Add avatar and memory controls**

Support initials, local preview/crop/upload, asynchronous stylized generation status, disclosure/cost, and prior-avatar preservation. Crop client-side with Canvas 2D: EXIF-oriented image, centered square, maximum 512×512, WebP quality 0.9; server normalization remains authoritative. Mock canvas/blob APIs in tests. Use `apiFormData` and `next/image` explicit sizes with `unoptimized`. Present memory by moving/reusing Release 2 `memory-list` logic inside `memory-panel`, including pin/unpin, promotion approve/reject, share/revoke, and irreversible forget confirmation; delete the duplicate old component/test only after migration.

- [ ] **Step 5: Retire the drawer as a settings surface**

Keep a compact Company preview if useful, but link to the full profile and do not maintain duplicate edit forms.

- [ ] **Step 6: Run agent gates and commit**

```bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
git add apps/web/app/'(app)'/agents apps/web/components/agents apps/web/components/avatar apps/web/components/memory apps/web/components/org/agent-drawer.tsx apps/web/lib/api.ts apps/web/lib/agent-profile.ts apps/web/lib/wizard.ts apps/web/tests
git commit -m "feat: make agents easy to create and understand"
```

### Task 8: Build the optional Company experience

**Files:**
- Create: `apps/web/app/(app)/company/layout.tsx`
- Replace temporary compatibility redirect: `apps/web/app/(app)/company/page.tsx`
- Create: `apps/web/app/(app)/company/teams/page.tsx`
- Create: `apps/web/app/(app)/company/teams/[teamId]/page.tsx`
- Create: `apps/web/app/(app)/company/map/page.tsx`
- Create: `apps/web/app/(app)/company/activity/page.tsx`
- Create: `apps/web/components/company/company-tabs.tsx`
- Create: `apps/web/components/company/company-overview.tsx`
- Create: `apps/web/components/company/team-directory.tsx`
- Create: `apps/web/components/company/team-profile.tsx`
- Create: `apps/web/components/company/org-map.tsx`
- Create: `apps/web/components/company/org-outline.tsx`
- Create: `apps/web/components/company/company-activity.tsx`
- Create: `apps/web/components/company/relationship-editor.tsx`
- Modify: `apps/web/lib/org-tree.ts`
- Modify: `apps/web/components/org/tree.tsx`
- Modify: `apps/web/components/org/team-dialog.tsx`
- Replace: `apps/web/app/(app)/organization/page.tsx`
- Create: `apps/web/tests/org-outline.test.tsx`
- Create: `apps/web/tests/company-activity.test.tsx`
- Modify: `apps/web/tests/org-tree.test.ts`
- Modify: `apps/web/tests/org-tree-render.test.tsx`

**Interfaces:**
- Company overview works for solo, peer, team, and nested-company workspaces.
- Tabs/routes: People at `/company` (with compact overview), Teams at `/company/teams`, Map at `/company/map`, and Activity at `/company/activity`; no duplicate People route.
- Semantic outline is always available and is mobile default; visual map is an alternate rendering of the same topology.

- [ ] **Step 1: Write failing Company tests**

Test solo/teamless state, flat peer group, multi-team agent, nested teams, optional manager, manager cycle display defense, close-collaborator/advisor/preferred-reviewer create/delete, team membership edits, viewer/member/admin action gating, activity filters, accessible tree keyboard behavior, exact tab routes, and equivalence between map/outline data.

```bash
pnpm --filter jhin-web test -- org-outline.test.tsx company-activity.test.tsx org-tree.test.ts org-tree-render.test.tsx
```

Expected: FAIL because the Company experience is absent and old tree assumes one team.

- [ ] **Step 2: Generalize topology projection**

Represent primary/secondary membership without duplicating identity; separate reporting edges from membership and collaborator edges. Defensive cycle handling produces a visible data error instead of recursion.

- [ ] **Step 3: Build overview, people, teams, and profiles**

Use optional-language empty states (`Create a team if it helps`) and never force setup. `/company` is the People directory plus compact counts/recent activity; team profile emphasizes purpose, members, current work, close collaborators, and activity. Admins edit memberships/relationships through Release 1 APIs; non-admins receive read-only explanations.

- [ ] **Step 4: Build accessible outline and optional map**

Outline uses semantic headings/tree controls and becomes default on narrow screens. Map uses names/roles plus labeled line styles; team color is decorative only.

- [ ] **Step 5: Redirect legacy Organization**

Server-redirect `/organization` to `/company/map` and migrate edit entry points to new Company/profile routes.

- [ ] **Step 6: Run Company gates and commit**

```bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
git add apps/web/app/'(app)'/company apps/web/app/'(app)'/organization apps/web/components/company apps/web/components/org apps/web/lib/org-tree.ts apps/web/tests
git commit -m "feat: add an optional AI company experience"
```

### Task 9: Make Apps, Automations, onboarding, members, and settings friendly

**Files:**
- Modify: `packages/db/src/jhin_db/models/org.py`
- Modify: `packages/db/src/jhin_db/models/__init__.py`
- Create: `packages/db/src/jhin_db/alembic/versions/20260817_0020_workspace_onboarding_completion.py`
- Create: `packages/db/tests/test_workspace_onboarding_model.py`
- Modify: `packages/db/tests/test_migration_graph.py`
- Modify: `apps/api/src/jhin_api/workspaces/schemas.py`
- Modify: `apps/api/src/jhin_api/workspaces/service.py`
- Modify: `apps/api/src/jhin_api/workspaces/router.py`
- Modify: `apps/api/src/jhin_api/auth/schemas.py`
- Modify: `apps/api/src/jhin_api/auth/service.py`
- Create: `apps/api/tests/test_workspace_onboarding_completion_unit.py`
- Modify: `tests/integration/test_release1_migrations.py`
- Replace temporary compatibility redirect: `apps/web/app/(app)/apps/page.tsx`
- Replace temporary compatibility redirect: `apps/web/app/(app)/automations/page.tsx`
- Create: `apps/web/app/(app)/welcome/page.tsx`
- Replace temporary compatibility redirect: `apps/web/app/(app)/members/page.tsx`
- Create: `apps/web/app/(app)/advanced/settings/page.tsx`
- Create: `apps/web/components/apps/apps-gallery.tsx`
- Create: `apps/web/components/apps/connect-app-flow.tsx`
- Create: `apps/web/components/automations/automation-card.tsx`
- Create: `apps/web/components/automations/automation-template-picker.tsx`
- Create: `apps/web/components/onboarding/onboarding-flow.tsx`
- Create: `apps/web/components/onboarding/provider-step.tsx`
- Create: `apps/web/components/onboarding/first-agent-step.tsx`
- Create: `apps/web/components/members/member-management.tsx`
- Create: `apps/web/components/advanced/advanced-settings-screen.tsx`
- Create: `apps/web/lib/onboarding.ts`
- Modify: `apps/web/lib/types.ts`
- Create: `apps/web/lib/queries/workspace.ts`
- Create: `apps/web/lib/automation-templates.ts`
- Modify: `apps/web/components/app-shell.tsx`
- Modify: `apps/web/components/connectors-gallery.tsx`
- Modify: `apps/web/app/setup/page.tsx`
- Modify: `apps/web/app/login/page.tsx`
- Modify: `apps/web/app/(app)/settings/page.tsx`
- Create: `apps/web/tests/apps-gallery.test.tsx`
- Create: `apps/web/tests/automations.test.tsx`
- Create: `apps/web/tests/onboarding.test.tsx`

**Interfaces:**
- First run: workspace name → AI provider → first agent purpose/access → first chat.
- Apps uses friendly service cards/connect flow; Automations uses templates before advanced conditions.
- Simple Settings contains profile/theme/preferences; concurrency/delegation/policy details move to Advanced.
- Onboarding completion is durable server state, not a browser heuristic. Migration revision `"0020"` has `down_revision="0019"` and adds nullable `Workspace.onboarding_completed_at: datetime | None`; the migration graph therefore has one linear head through `0017 -> 0018 -> 0019 -> 0020`.
- `POST /api/v1/workspaces/{workspace_id}/complete-onboarding` is an authenticated, CSRF-protected admin endpoint. On the first successful call it locks/loads the workspace, requires one enabled provider with `last_verified_at` and a same-workspace `ModelProfile` with a nonblank `model_name`, plus one `AgentStatus.ACTIVE` agent in that workspace, then writes and returns `onboarding_completed_at`. Once stored, it is idempotent and returns the existing timestamp without re-validating current provider/agent state, so a workspace that was completed does not get trapped in Welcome if a provider or agent is later disabled.
- `WorkspaceOut` and the authenticated membership/bootstrap projection expose `onboarding_completed_at`. `useWorkspaceOnboarding` fetches the authoritative workspace record, calls the completion endpoint only after provider and active-agent mutations settle, and invalidates the workspace/auth query. The app shell (and login's post-auth destination) routes authenticated users to `/welcome` only when that timestamp is null; `/welcome` redirects an already-complete workspace to `/chats`. This works across devices and never treats local storage as completion.
- Built-in automation templates are typed constants for `event_to_agent` and `engineering_ticket`; both compile to the existing trigger/workflow API and no unsupported schedule trigger is advertised.

- [ ] **Step 1: Write failing persistence, API, migration, and flow tests**

Write database tests for the nullable timestamp/default and upgrade/downgrade. Write API tests that prove: incomplete workspaces receive 422 with a safe provider/agent requirement before any timestamp is written; a verified enabled provider with a usable profile plus an active agent writes one timestamp; a repeat call preserves that exact timestamp; disabling the provider or agent afterward still returns the stored completion; a non-admin receives 403; and another workspace cannot satisfy the predicate. Extend `test_migration_graph.py` and `tests/integration/test_release1_migrations.py` to upgrade a fresh and a prior-`0019` disposable database through `0020`, assert the new column, downgrade/re-upgrade, and retain the full `0017 -> 0018 -> 0019 -> 0020` chain.

Then cover server-derived first-run completion across a second browser, shell/login redirect behavior for null versus stored timestamp, local in-step resume/back behavior, provider failure guidance, first agent without team/manager, access summary, first chat routing, Apps search/connect/disconnect consequence, exact automation template compilation/create/pause, viewer/member/admin gating across Apps/Automations/Members/Settings, member roles, and Settings/Advanced separation.

```bash
uv run pytest packages/db/tests/test_workspace_onboarding_model.py packages/db/tests/test_migration_graph.py apps/api/tests/test_workspace_onboarding_completion_unit.py -q
uv run pytest -m integration tests/integration/test_release1_migrations.py -v
pnpm --filter jhin-web test -- apps-gallery.test.tsx automations.test.tsx onboarding.test.tsx
```

Expected: FAIL for the missing `0020` schema/endpoint and friendly flows.

- [ ] **Step 2: Add durable completion persistence and endpoint**

Add `Workspace.onboarding_completed_at` to the SQLAlchemy model and `WorkspaceOut`; create `20260817_0020_workspace_onboarding_completion.py` with exactly `revision = "0020"` and `down_revision = "0019"`, an additive nullable UTC timestamp, and a downgrade that drops only that column. Implement `complete_onboarding(db, ctx, request_id, ip_hash) -> Workspace` in the workspace service. If the timestamp exists, return it unchanged. Otherwise query a workspace-scoped `ModelProvider` joined to a same-workspace `ModelProfile`, require `enabled`, non-null `last_verified_at`, and nonblank `model_name`, then query one active agent, set `onboarding_completed_at` once, record `workspace.onboarding_completed`, and commit. Expose it from the router as `POST /{workspace_id}/complete-onboarding` using `AdminCtx` and the existing CSRF dependency. Add its timestamp to the auth membership/bootstrap projection so shell/login can decide before rendering ordinary navigation; do not introduce a production test shortcut or infer completion from client state.

Run the focused red-to-green backend gates:

```bash
uv run pytest packages/db/tests/test_workspace_onboarding_model.py packages/db/tests/test_migration_graph.py apps/api/tests/test_workspace_onboarding_completion_unit.py -q
uv run pytest -m integration tests/integration/test_release1_migrations.py -v
```

Expected: PASS, including fresh/prior-head upgrade and downgrade/re-upgrade through `0020`.

- [ ] **Step 3: Build progressive first-run onboarding and shell guard**

Reuse setup/provider/agent APIs and derive completion from the fresh authoritative workspace query after login. Persist only the current incomplete step as a non-sensitive local hint; server state always wins. After a successful provider verification and active-agent creation, invoke `complete-onboarding`; show the API's actionable 422 message while either prerequisite is missing. Do not offer teams/hierarchy as required onboarding steps. Modify `components/app-shell.tsx` in this task to keep shell content behind the timestamp-based guard, and update login's post-auth routing with the same authoritative field. If a provider or agent is later disabled, ordinary shell remains usable and shows Needs your attention rather than trapping an established workspace back in onboarding.

- [ ] **Step 4: Build Apps and Automations facades**

Wrap existing connector/trigger capabilities with plain labels, consequences, typed templates, and safe defaults. Until Task 10 creates the final Advanced Apps/Automations routes, technical links point to the still-functional `/connectors` and `/triggers` routes; Task 10 updates them atomically with redirects. Do not create broken intermediate links.

- [ ] **Step 5: Split members and settings**

Extract the complete existing member list/invite/role/remove implementation from Settings into `MemberManagement` and mount it on `/members`; no duplicate member mutation code remains. Keep workspace name/theme/preferences in Settings. Create `/advanced/settings` now and move technical workspace/concurrency/delegation controls there before removing them from old surfaces.

- [ ] **Step 6: Run flow, backend, migration, and integration gates and commit**

```bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
uv run pytest packages/db/tests/test_workspace_onboarding_model.py packages/db/tests/test_migration_graph.py apps/api/tests/test_workspace_onboarding_completion_unit.py -q
uv run pytest -m integration tests/integration/test_release1_migrations.py -v
git add packages/db/src/jhin_db/models/org.py packages/db/src/jhin_db/models/__init__.py packages/db/src/jhin_db/alembic/versions/20260817_0020_workspace_onboarding_completion.py packages/db/tests/test_workspace_onboarding_model.py packages/db/tests/test_migration_graph.py apps/api/src/jhin_api/workspaces apps/api/src/jhin_api/auth apps/api/tests/test_workspace_onboarding_completion_unit.py tests/integration/test_release1_migrations.py apps/web/app apps/web/components/app-shell.tsx apps/web/components/apps apps/web/components/automations apps/web/components/onboarding apps/web/components/members apps/web/components/advanced/advanced-settings-screen.tsx apps/web/components/connectors-gallery.tsx apps/web/lib/onboarding.ts apps/web/lib/types.ts apps/web/lib/queries/workspace.ts apps/web/lib/automation-templates.ts apps/web/tests
git commit -m "feat: simplify setup apps and automations"
```

### Task 10: Move all operational surfaces under Advanced with compatibility redirects

**Files:**
- Create: `apps/web/app/(app)/advanced/layout.tsx`
- Create: `apps/web/app/(app)/advanced/page.tsx`
- Create: `apps/web/app/(app)/advanced/work/page.tsx`
- Create: `apps/web/app/(app)/advanced/work/[taskId]/page.tsx`
- Create: `apps/web/app/(app)/advanced/runs/page.tsx`
- Create: `apps/web/app/(app)/advanced/models/page.tsx`
- Create: `apps/web/app/(app)/advanced/apps/page.tsx`
- Create: `apps/web/app/(app)/advanced/automations/page.tsx`
- Modify: `apps/web/app/(app)/advanced/attention/page.tsx`
- Create: `apps/web/app/(app)/advanced/audit/page.tsx`
- Modify: `apps/web/app/(app)/advanced/system/page.tsx`
- Modify: `apps/web/app/(app)/advanced/settings/page.tsx`
- Create: `apps/web/app/(app)/advanced/tools/page.tsx`
- Create: `apps/web/app/(app)/advanced/policies/page.tsx`
- Create: `apps/web/components/advanced/work-queue-screen.tsx`
- Create: `apps/web/components/advanced/work-detail-screen.tsx`
- Create: `apps/web/components/advanced/run-explorer-screen.tsx`
- Create: `apps/web/components/advanced/model-management-screen.tsx`
- Create: `apps/web/components/advanced/app-diagnostics-screen.tsx`
- Create: `apps/web/components/advanced/automation-rules-screen.tsx`
- Create: `apps/web/components/advanced/audit-screen.tsx`
- Create: `apps/web/components/advanced/tools-grants-screen.tsx`
- Create: `apps/web/components/advanced/review-policy-screen.tsx`
- Create: `apps/web/components/advanced/responsive-record-list.tsx`
- Replace with redirects: `apps/web/app/(app)/tasks/page.tsx`
- Replace with redirects: `apps/web/app/(app)/tasks/[id]/page.tsx`
- Replace with redirects: `apps/web/app/(app)/runs/page.tsx`
- Replace with redirects: `apps/web/app/(app)/models/page.tsx`
- Replace with redirects: `apps/web/app/(app)/connectors/page.tsx`
- Replace with redirects: `apps/web/app/(app)/triggers/page.tsx`
- Replace with redirects: `apps/web/app/(app)/audit/page.tsx`
- Modify: `apps/web/components/task-bits.tsx`
- Modify: `apps/web/components/org/agent-drawer.tsx`
- Modify: `apps/web/components/advanced/approval-evidence-card.tsx`
- Modify: `apps/web/app/(app)/triggers/page.tsx`
- Modify: `apps/web/app/(app)/runs/page.tsx`
- Modify: `apps/web/app/(app)/tasks/[id]/page.tsx`
- Create: `apps/web/tests/advanced-navigation.test.tsx`

**Interfaces:**
- Advanced Work, Runs, Models, Apps diagnostics, Automation rules, Attention evidence, Tools/grants, review policies, Audit, System health, and Settings preserve the old functions.
- Old paths redirect to exact equivalent destinations, including task IDs.

- [ ] **Step 1: Write failing Advanced/compatibility tests**

Inventory every old page action and assert an equivalent Advanced destination. Test route redirects, exact task-ID preservation, preservation of `/tasks?agent=`, `/runs?agent=`, and other recognized filters, `/agents/new?team=` builder compatibility, literal-link migration in task bits/approval evidence/agent drawer/run/trigger/task-detail, raw evidence availability, viewer/member/admin gating, and absence of Advanced items from default nav.

```bash
pnpm --filter jhin-web test -- advanced-navigation.test.tsx
```

Expected: FAIL because the Advanced route group is absent.

- [ ] **Step 2: Extract rather than duplicate operational screens**

Perform exact extractions: old Tasks → `WorkQueueScreen`; task detail → `WorkDetailScreen`; Runs → `RunExplorerScreen`; Models → `ModelManagementScreen`; Connectors → `AppDiagnosticsScreen`; Triggers → `AutomationRulesScreen`; Audit → `AuditScreen`. Existing System, Attention evidence, and Advanced Settings components were extracted earlier and are extended, not duplicated. Tools/grants moves from the agent drawer into `ToolsGrantsScreen`; Release 3 review-policy CRUD mounts in `ReviewPolicyScreen`. Route pages become thin imports, then old pages become redirects.

- [ ] **Step 3: Create the Advanced index and navigation**

Explain each destination in plain language, preserve direct deep links, and include a clear return to the ordinary product. Monospace/raw identifiers are permitted only here. Owner/admin may mutate tools/grants/policies; viewer/member visibility follows the existing RBAC/read contracts.

- [ ] **Step 4: Make operational records responsive**

Wrap every Advanced table in `ResponsiveRecordList`: semantic table at wide widths and equivalent labeled record cards below 720px. Both renderers receive the same column descriptors/actions, preserve keyboard order and status text, and never rely on horizontal scrolling as the only mobile access.

- [ ] **Step 5: Replace legacy pages with server redirects**

Use central route helpers for links and exact dynamic-param forwarding. Server pages await both `params` and `searchParams`, allowlist/encode supported filters, and pass them to the equivalent Advanced route. Do not use client effects for redirects.

- [ ] **Step 6: Run full frontend/build gates and commit**

```bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web exec next build --webpack
git add apps/web
git commit -m "feat: preserve operational power under Advanced"
```

### Task 11: Add browser, accessibility, responsive, and visual acceptance coverage

**Files:**
- Modify: `apps/web/package.json`
- Modify: `package.json`
- Modify: `pnpm-lock.yaml`
- Create: `apps/web/playwright.config.ts`
- Create: `apps/web/tests/e2e/global-setup.ts`
- Create: `apps/web/tests/e2e/global-teardown.ts`
- Create: `apps/web/tests/e2e/support/provision-state.ts`
- Create: `apps/web/tests/e2e/fixtures.ts`
- Create: `apps/web/tests/e2e/experience-desktop.spec.ts`
- Create: `apps/web/tests/e2e/experience-mobile.spec.ts`
- Create: `apps/web/tests/e2e/accessibility.spec.ts`
- Create: `apps/web/tests/e2e/theme.spec.ts`
- Create: `apps/web/tests/e2e/legacy-routes.spec.ts`
- Create: `apps/web/tests/e2e/rbac.spec.ts`
- Create: `apps/web/tests/e2e/__screenshots__/README.md`
- Create: `apps/api/src/jhin_api/testing/__init__.py`
- Create: `apps/api/src/jhin_api/testing/e2e_provision.py`
- Create: `apps/api/tests/test_e2e_provision.py`
- Modify: `apps/api/pyproject.toml`
- Modify: `apps/web/.gitignore`
- Create: `docs/qa/screen-reader-smoke-2026-08-17.md`
- Create: `docs/qa/mobile-safe-area-smoke-2026-08-17.md`
- Create: `docs/architecture/chat-first-experience.md`
- Modify: `docs/implementation-plan.md`

**Interfaces:**
- Playwright projects for desktop, tablet, and mobile.
- axe checks plus explicit keyboard/focus/reduced-motion/zoom flows.
- Screenshot baselines under `apps/web/tests/e2e/__screenshots__/{projectName}/` for shell, chat, agent profile, Company, Attention, Advanced responsive records, and warm-dark theme.
- E2E identity provisioning is a test-only CLI/factory, never a public registration endpoint. It creates exact unique users with the production password hasher, and global setup records every created user ID and workspace ID for exact teardown.

- [ ] **Step 1: Add failing end-to-end specs**

Cover onboarding to first chat; new/renamed/multi-turn chat; agent-agent handoff visibility; attention decision; agent create/avatar/memory controls; optional team/company map and outline; Apps/Automations; owner/admin/member/viewer UI/action boundaries; Advanced legacy access/filter redirects; narrow-screen record cards; no forced chat scroll; desktop/tablet/mobile shell composition; drawer focus; theme persistence; reduced motion; and the four-part copy for memory/provider/reviewer/revocation failures.

- [ ] **Step 2: Add a test-only E2E user factory and global lifecycle**

Create `jhin_api.testing.e2e_provision` and expose it only as the `jhin-e2e-provision` CLI in `apps/api/pyproject.toml`; do not import it from `main.py`, add a router, or document it as an application API. Every command must fail unless `APP_ENV == "test"`, and both create and delete require an email beginning with the case-normalized `e2e-` prefix. Its create operation accepts explicit `--email`, `--password`, and `--display-name`, rejects an existing email, calls the real `jhin_api.security.passwords.hash_password`, flushes/commits one `User`, and prints only structured `{id,email}` JSON. Its delete operation accepts the exact recorded user ID and matching prefixed email and deletes only that user after its test workspace is gone. Unit-test the environment guard, prefix guard, duplicate protection, password-hash verification, and exact-ID cleanup.

Implement `global-setup.ts` to generate one run nonce and four exact addresses such as `e2e-${nonce}-owner@example.test`; invoke the CLI four times to create owner, admin, member, and viewer with known test passwords. It then uses normal public login/workspace/member APIs—not a public registration API and not direct database inserts—to log in as the owner, create one uniquely named workspace, add the three recorded users with admin/member/viewer roles, and write the returned user IDs, workspace ID, membership IDs, owner storage/cookie state, and nonce to an ignored `test-results/e2e-provision.json` file. Fixtures read this record and seed scenario data only through authenticated public APIs. `global-teardown.ts` first uses the owner cookie and public `DELETE /workspaces/{workspaceId}`, then invokes the guarded CLI once per recorded exact user ID/email, verifies the recorded state belongs to the nonce, and removes only that state file. A failed setup must run the same best-effort exact cleanup before rethrowing. No production endpoint, registration assumption, broad email query, or broad delete is permitted.

- [ ] **Step 3: Install and configure the acceptance harness**

Add `@playwright/test` and `@axe-core/playwright` (user-event was installed in Task 3). Configure `globalSetup`, `globalTeardown`, `webServer.command = "pnpm --filter jhin-web exec next dev --webpack -H 127.0.0.1 -p 3100"`, URL `http://127.0.0.1:3100`, and the existing Compose API/backend. Set the snapshot location exactly to `snapshotPathTemplate: "{testDir}/__screenshots__/{projectName}/{testFilePath}/{arg}{ext}"`, which yields the required `apps/web/tests/e2e/__screenshots__/{projectName}/...` hierarchy. Configure trace/screenshot/video on failure, local fonts, reduced/disabled deterministic animation, and desktop 1440×1000, tablet 768×1024, mobile 390×844 projects. The fixture consumes the lifecycle state from Step 2 and uses those four roles rather than registering accounts itself.

- [ ] **Step 4: Run component and production-build gates**

```bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web exec next build --webpack
```

Expected: PASS.

- [ ] **Step 5: Run desktop/tablet/mobile and accessibility suites**

```bash
pnpm --filter jhin-web exec playwright test
```

Expected: PASS with no serious/critical axe violations and all keyboard/touch flows green.

- [ ] **Step 6: Perform visual QA at representative sizes**

Inspect screenshots at 1440×1000, 1024×900, 768×1024, and 390×844 in light and warm-dark themes. Fix clipping, contrast, overflow, ambiguous state, and inconsistent spacing before recording approval.

- [ ] **Step 7: Complete assistive-technology and real-device smoke checks**

Run VoiceOver on macOS through onboarding, chat/composer/new updates, agent creation/avatar, Attention decision, memory controls, Company outline, dialog/drawer, and Advanced return navigation; record date/browser/steps/results in the QA artifact. Add Playwright accessibility-tree snapshots for the same landmarks and decision names. Run the mobile flow in iOS Safari on a connected device or Simulator with the onscreen keyboard open, portrait/landscape, and safe areas; record screenshots/results. Include an NVDA + Firefox/Windows matrix for a release operator and clearly mark it not executed if that environment is unavailable—never report an unrun matrix as passing.

- [ ] **Step 8: Run complete repository/integration gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not integration"
uv run pytest packages/db/tests/test_migration_graph.py -q
uv run pytest -m integration tests/integration/test_phase8_exit.py tests/integration/test_company_identity_exit.py tests/integration/test_release1_migrations.py tests/integration/test_release2_conversations.py tests/integration/test_release2_memory.py tests/integration/test_release3_coordination.py -v
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web exec next build --webpack
pnpm --filter jhin-web exec playwright test
```

Expected: PASS.

- [ ] **Step 9: Document the redesign and commit final evidence**

Document IA, route mapping, visual tokens, responsive behavior, accessibility decisions, VoiceOver/iOS evidence and any explicitly unrun matrix, activity presentation, Advanced preservation, test counts, screenshot review, and any environment-only Turbopack limitation. Mark only completed Release 4 items.

```bash
git add apps/web/package.json package.json pnpm-lock.yaml apps/web/playwright.config.ts apps/web/.gitignore apps/web/tests/e2e apps/api/pyproject.toml apps/api/src/jhin_api/testing apps/api/tests/test_e2e_provision.py docs/qa docs/architecture/chat-first-experience.md docs/implementation-plan.md
git commit -m "docs: verify the chat-first Jhin experience"
```
