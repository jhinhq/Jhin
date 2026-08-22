# Jhin web redesign brief (supersedes the visual system in the 2026-08-17 spec)

The product UI is based on the Jhin landing page (`../Jhin-Landing`): the
"Pastel Skies" palette, the isometric cube J mark, Inter for body, Space
Grotesk for display, JetBrains Mono for code. Light-first with a dark theme.
Friendly and conversational by default; operational machinery lives under
**Advanced**.

## Tokens (apps/web/app/globals.css)

Keep the existing Tailwind color token **names** so every page inherits the
new look: `bg`, `surface`, `raised`, `hover`, `line`, `line-strong`, `ink`,
`dim`, `faint`, `accent`, `accent-strong`, `accent-soft`, `ok`, `warn`,
`danger`. Add `accent-2`, `glow`, `ok-soft`, `warn-soft`, `danger-soft`,
`info`, `info-soft`.

Light (`:root`):

```
--bg #faf7ff         --surface #ffffff     --surface-raised #f5efff
--surface-hover #efe9ff   --line rgba(115,113,252,0.16)  --line-strong rgba(115,113,252,0.32)
--text #221e38       --text-dim #5f5a85    --text-faint #8f89b3
--accent #7371fc     --accent-strong #5a58e8   --accent-2 #a594f9
--accent-soft rgba(115,113,252,0.12)   --glow rgba(165,148,249,0.35)
--ok #2e7558  --ok-soft #e7f4ec   --warn #985b08  --warn-soft #fff2d8
--danger #b44351  --danger-soft #fce9eb   --info #316f98  --info-soft #e8f4fa
--card-shadow 0 1px 2px rgba(34,30,56,0.04), 0 12px 40px -12px rgba(115,113,252,0.18)
```

Dark (`.dark` on `<html>`, toggled like the landing page with
`localStorage["jhin-theme"]` and a no-flash inline script in `app/layout.tsx`):

```
--bg #0c0a17   --surface #161226   --surface-raised #1c1730   --surface-hover #241d3d
--line rgba(205,193,255,0.13)   --line-strong rgba(205,193,255,0.3)
--text #f5efff   --text-dim #b9b1dc   --text-faint #8a83ad
--accent #a594f9   --accent-strong #cdc1ff   --accent-2 #cdc1ff
--accent-soft rgba(165,148,249,0.16)   --glow rgba(115,113,252,0.4)
--ok #5fd3a0  --ok-soft rgba(62,207,142,0.14)   --warn #f5b453  --warn-soft rgba(245,180,83,0.14)
--danger #f0596b  --danger-soft rgba(240,89,107,0.14)   --info #7cc0ea  --info-soft rgba(49,111,152,0.2)
```

Fonts: add `@fontsource-variable/inter`, `@fontsource-variable/space-grotesk`,
`@fontsource-variable/jetbrains-mono` to `apps/web` and import them in
`app/layout.tsx`. `--font-sans` = Inter, `--font-display` = Space Grotesk,
`--font-mono` = JetBrains Mono. Expose `font-display` and `font-mono`
utilities through `@theme inline`.

Shape: cards `rounded-2xl` with `border-line` + `--card-shadow`; inputs and
buttons `rounded-xl`; pills `rounded-full`. Primary button = gradient
`linear-gradient(120deg, var(--accent), var(--accent-2))`, white text,
`--glow` shadow, `hover:-translate-y-px`. Ghost/outline buttons as on the
landing page. Body 15–16px, metadata ≥13px, generous whitespace. Motion is
small and purposeful; honor `prefers-reduced-motion`. Min interactive target
40px (44px on touch).

Brand: `components/brand/logo-mark.tsx` exports `LogoMark` and `Wordmark`;
`public/mark.svg`, `public/logo.svg`, `app/icon.svg` are copied from the
landing page.

## Shell and navigation (`components/app-shell.tsx`)

Desktop (≥1024px): left sidebar 260px, workspace switcher/wordmark at top,
primary nav, then an **Advanced** group (collapsible, remembers state in
localStorage `jhin-advanced-open`), then user/theme/sign-out at the bottom.
Tablet (768–1023px): 72px icon rail with tooltips. Mobile (<768px): top bar
with wordmark + attention badge, bottom tab bar with Chats, Agents, Company,
Activity, More (More opens a sheet with the rest).

Primary nav (friendly):

| label | href | icon (lucide) | notes |
| --- | --- | --- | --- |
| Chats | `/chats` | MessageSquare | default destination; `/` redirects here |
| Agents | `/agents` | Bot | directory + profiles |
| Company | `/company` | Building2 | org map, teams |
| Activity | `/activity` | Radio | agent-to-agent feed |
| Attention | `/attention` | BellRing | badge = attention counts.total |
| Automations | `/automations` | Zap | friendly wrapper for triggers |
| Apps | `/apps` | Plug | friendly wrapper for connectors |

Advanced group (existing operational pages, unchanged routes):
Work queue `/tasks`, Runs `/runs`, Approvals `/approvals`, Connectors
`/connectors`, Triggers `/triggers`, Models `/models`, Audit `/audit`,
Settings `/settings`, plus `/advanced` as an index page explaining each.

`PageHeader` stays exported with the same props (existing pages use it) and
gets the new styling; add an optional `eyebrow` prop and `compact` prop.

## Copy rules for default screens

No raw event names, workflow ids, capability strings, or UUIDs unless the
user expands "Details" or is in Advanced. Statuses always pair color with
text. Every error states what failed and a safe next step.

## Route ownership

- Shell, theme, fonts, primitives, auth/setup, `/advanced`, restyle pass of
  existing pages: **design-system agent**.
- `/chats`, `/chats/[id]`, chat components under `components/chat/`: **chat
  agent**.
- `/agents`, `/agents/[id]`, `/company`, `/activity`, `/attention`,
  `/automations`, `/apps`, components under `components/company/`,
  `components/activity/`: **company agent**.

Shared, already present: `components/avatar.tsx` (`Avatar`, `initialsOf`),
`components/brand/logo-mark.tsx`, conversation/activity types in
`lib/types.ts`, hooks in `lib/hooks.ts` (`useConversations`,
`useConversation`, `useConversationMessages`, `useConversationActivity`,
`useActivity`, `useAttention`, `useInvalidateConversations`). API contract:
`docs/architecture/conversations.md`.
