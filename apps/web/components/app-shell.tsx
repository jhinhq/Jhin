"use client";

/** Authenticated application shell (2026-08-21 redesign brief): responsive
 * sidebar / icon rail / mobile tab bar, plus the auth guard. Unauthenticated
 * visitors are redirected to /login (or /setup on first run). */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  BellRing,
  BookOpen,
  Bot,
  Building2,
  CheckSquare,
  ChevronDown,
  Cpu,
  House,
  KeyRound,
  ListTodo,
  LogOut,
  MessageSquare,
  MoreHorizontal,
  Plug,
  Radio,
  ScrollText,
  Settings,
  ShieldCheck,
  Users,
  Wrench,
  Zap,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore } from "react";
import type { LucideIcon } from "lucide-react";
import { Wordmark } from "@/components/brand/logo-mark";
import { OnboardingProvider } from "@/components/onboarding/tour";
import { Button, Dialog, ErrorNote, Field, focusRing, Input, Spinner, ThemeToggle } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { goToConnect, IS_DESKTOP } from "@/lib/desktop";
import { useAttention, useBootstrapStatus, useIdentity } from "@/lib/hooks";
import { useWorkspace, WorkspaceProvider } from "@/lib/workspace-context";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /** One-line description shown on /advanced and the mobile "More" sheet. */
  description?: string;
}

const PRIMARY_NAV: readonly NavItem[] = [
  { href: "/home", label: "Home", icon: House },
  { href: "/chats", label: "Chats", icon: MessageSquare },
  { href: "/agents", label: "Agents", icon: Bot },
  { href: "/company", label: "Company", icon: Building2 },
  { href: "/activity", label: "Activity", icon: Radio },
  { href: "/attention", label: "Attention", icon: BellRing },
  { href: "/automations", label: "Automations", icon: Zap },
  { href: "/apps", label: "Apps", icon: Plug },
  { href: "/models", label: "Models", icon: Cpu },
  { href: "/skills", label: "Skills", icon: BookOpen },
] as const;

export const ADVANCED_NAV: readonly NavItem[] = [
  {
    href: "/tasks",
    label: "Work queue",
    icon: ListTodo,
    description: "Every task your agents have been given, with its current state.",
  },
  {
    href: "/runs",
    label: "Runs",
    icon: Activity,
    description: "Individual agent executions, their steps, and what they cost.",
  },
  {
    href: "/review-policies",
    label: "Review policies",
    icon: ShieldCheck,
    description: "When an agent's work gets a second look before it goes further, and by whom.",
  },
  {
    href: "/approvals",
    label: "Approvals",
    icon: CheckSquare,
    description: "Actions that paused to wait for a person to say yes or no.",
  },
  {
    href: "/audit",
    label: "Audit",
    icon: ScrollText,
    description: "A permanent record of who changed what, and when.",
  },
  {
    href: "/people",
    label: "People",
    icon: Users,
    description: "Who can use this workspace, what each role may do, and pending invitations.",
  },
  {
    href: "/api-keys",
    label: "API keys",
    icon: KeyRound,
    description: "Scoped keys for scripts and other systems, and a log of what they called.",
  },
  {
    href: "/settings",
    label: "Settings",
    icon: Settings,
    description: "Workspace name, model budget, and general preferences.",
  },
] as const;

/** Mobile bottom tab bar shows the first four primary items + "More". */
const MOBILE_TABS = PRIMARY_NAV.slice(0, 4);

const ADVANCED_STORAGE_KEY = "jhin-advanced-open";

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

function CountBadge({ count, tone = "warn" }: { count: number; tone?: "warn" | "accent" }) {
  if (count <= 0) return null;
  const colors =
    tone === "warn" ? "bg-warn-soft text-warn" : "bg-accent-soft text-accent-strong";
  return (
    <span
      data-testid="nav-badge"
      className={`inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-[11px] font-semibold tabular-nums ${colors}`}
    >
      {count > 99 ? "99+" : count}
    </span>
  );
}

function NavLink({
  item,
  active,
  badge,
  rail = false,
}: {
  item: NavItem;
  active: boolean;
  badge?: number;
  rail?: boolean;
}) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      title={item.label}
      aria-current={active ? "page" : undefined}
      className={`relative flex min-h-10 items-center gap-3 rounded-xl text-sm transition-colors ${focusRing} ${
        rail ? "justify-center px-0" : "px-3"
      } ${
        active
          ? "bg-accent-soft font-semibold text-accent-strong"
          : "text-dim hover:bg-hover hover:text-ink"
      }`}
    >
      <Icon size={18} strokeWidth={1.8} className="shrink-0" aria-hidden />
      <span className={`flex-1 truncate ${rail ? "sr-only" : ""}`}>{item.label}</span>
      {badge !== undefined && badge > 0 ? (
        rail ? (
          <span
            aria-label={`${badge} items need attention`}
            className="absolute right-2 top-1.5 h-2 w-2 rounded-full bg-warn"
          />
        ) : (
          <CountBadge count={badge} />
        )
      ) : null}
    </Link>
  );
}

const ADVANCED_EVENT = "jhin-advanced-change";

function readAdvancedOpen(): boolean {
  try {
    return localStorage.getItem(ADVANCED_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}
function subscribeAdvanced(onChange: () => void) {
  window.addEventListener(ADVANCED_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(ADVANCED_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}
function useAdvancedOpen(): [boolean, (next: boolean) => void] {
  const open = useSyncExternalStore(subscribeAdvanced, readAdvancedOpen, () => false);
  const update = (next: boolean) => {
    try {
      localStorage.setItem(ADVANCED_STORAGE_KEY, String(next));
    } catch {
      /* storage unavailable */
    }
    window.dispatchEvent(new Event(ADVANCED_EVENT));
  };
  return [open, update];
}

/** Sidebar / rail nav; lives inside WorkspaceProvider so the attention badge
 * can poll. `rail` renders icon-only links with native title tooltips. */
export function SidebarNav({ pathname, rail = false }: { pathname: string; rail?: boolean }) {
  const { workspace } = useWorkspace();
  const attention = useAttention(workspace.workspace_id);
  const attentionCount = attention.data?.counts.total ?? 0;
  const [advancedOpen, setAdvancedOpen] = useAdvancedOpen();
  const advancedActive = ADVANCED_NAV.some((item) => isActive(pathname, item.href));
  // Landing on an Advanced page (deep link, "Open in Advanced") should reveal
  // where you are in the sidebar instead of leaving the group collapsed.
  useEffect(() => {
    if (advancedActive && !advancedOpen) setAdvancedOpen(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only react to navigation
  }, [pathname, advancedActive]);

  return (
    <nav aria-label="Main" className={`flex-1 overflow-y-auto py-2 ${rail ? "px-3" : "px-3"}`}>
      <ul className="space-y-0.5">
        {PRIMARY_NAV.map((item) => (
          <li key={item.href}>
            <NavLink
              item={item}
              rail={rail}
              active={isActive(pathname, item.href)}
              badge={item.href === "/attention" ? attentionCount : undefined}
            />
          </li>
        ))}
      </ul>

      <div className="my-3 border-t border-line" role="presentation" />

      {rail ? (
        <ul className="space-y-0.5">
          <li>
            <NavLink
              item={{ href: "/advanced", label: "Advanced", icon: Wrench }}
              rail
              active={advancedActive || isActive(pathname, "/advanced")}
            />
          </li>
        </ul>
      ) : (
        <div>
          <button
            type="button"
            aria-expanded={advancedOpen}
            aria-controls="advanced-nav"
            onClick={() => setAdvancedOpen(!advancedOpen)}
            className={`flex min-h-10 w-full items-center gap-3 rounded-xl px-3 text-[13px] font-semibold uppercase tracking-wider transition-colors hover:bg-hover ${focusRing} ${
              advancedActive ? "text-accent-strong" : "text-faint hover:text-ink"
            }`}
          >
            <Wrench size={16} strokeWidth={1.8} aria-hidden />
            <span className="flex-1 text-left">Advanced</span>
            <ChevronDown
              size={16}
              aria-hidden
              className={`transition-transform ${advancedOpen ? "rotate-180" : ""}`}
            />
          </button>
          {advancedOpen ? (
            <ul id="advanced-nav" className="mt-0.5 space-y-0.5">
              {ADVANCED_NAV.map((item) => (
                <li key={item.href}>
                  <NavLink item={item} active={isActive(pathname, item.href)} />
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      )}
    </nav>
  );
}

function MobileBars({ pathname, onSignOut }: { pathname: string; onSignOut: () => void }) {
  const { workspace, user } = useWorkspace();
  const attention = useAttention(workspace.workspace_id);
  const attentionCount = attention.data?.counts.total ?? 0;
  const [moreOpen, setMoreOpen] = useState(false);
  const moreItems = [...PRIMARY_NAV.slice(4), ...ADVANCED_NAV];
  const moreActive = moreItems.some((item) => isActive(pathname, item.href));

  const closeMore = () => setMoreOpen(false);

  return (
    <>
      <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-line bg-bg/85 px-4 backdrop-blur md:hidden">
        <Link href="/home" className={`rounded-lg ${focusRing}`} aria-label="Jhin home">
          <Wordmark />
        </Link>
        <div className="flex items-center gap-2">
          <Link
            href="/attention"
            aria-label={
              attentionCount > 0 ? `Attention: ${attentionCount} items` : "Attention"
            }
            className={`relative inline-flex h-10 w-10 items-center justify-center rounded-xl text-dim hover:bg-hover hover:text-ink ${focusRing}`}
          >
            <BellRing size={18} strokeWidth={1.8} aria-hidden />
            {attentionCount > 0 ? (
              <span className="absolute -right-0.5 -top-0.5">
                <CountBadge count={attentionCount} />
              </span>
            ) : null}
          </Link>
        </div>
      </header>

      <nav
        aria-label="Primary"
        className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-surface/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden"
      >
        <ul className="grid grid-cols-5">
          {MOBILE_TABS.map((item) => {
            const Icon = item.icon;
            const active = isActive(pathname, item.href);
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={`flex min-h-14 flex-col items-center justify-center gap-1 text-[11px] font-medium ${focusRing} ${
                    active ? "text-accent-strong" : "text-dim"
                  }`}
                >
                  <Icon size={20} strokeWidth={1.8} aria-hidden />
                  {item.label}
                </Link>
              </li>
            );
          })}
          <li>
            <button
              type="button"
              onClick={() => setMoreOpen(true)}
              aria-haspopup="dialog"
              aria-expanded={moreOpen}
              className={`flex min-h-14 w-full flex-col items-center justify-center gap-1 text-[11px] font-medium ${focusRing} ${
                moreActive ? "text-accent-strong" : "text-dim"
              }`}
            >
              <MoreHorizontal size={20} strokeWidth={1.8} aria-hidden />
              More
            </button>
          </li>
        </ul>
      </nav>

      <Dialog title="More" open={moreOpen} onClose={closeMore}>
        <ul className="grid grid-cols-2 gap-2">
          {moreItems.map((item) => {
            const Icon = item.icon;
            const active = isActive(pathname, item.href);
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  onClick={closeMore}
                  aria-current={active ? "page" : undefined}
                  className={`flex min-h-11 items-center gap-3 rounded-xl border px-3 text-sm ${focusRing} ${
                    active
                      ? "border-accent/40 bg-accent-soft font-semibold text-accent-strong"
                      : "border-line text-ink hover:bg-hover"
                  }`}
                >
                  <Icon size={18} strokeWidth={1.8} aria-hidden />
                  <span className="truncate">{item.label}</span>
                  {item.href === "/attention" ? <CountBadge count={attentionCount} /> : null}
                </Link>
              </li>
            );
          })}
        </ul>
        <div className="mt-4 flex items-center justify-between border-t border-line pt-4">
          <span className="text-sm text-dim">Theme</span>
          <ThemeToggle showLabel />
        </div>
        <div className="mt-3 flex items-center justify-between">
          <span className="truncate text-sm text-dim">Signed in as {user.display_name}</span>
          <button
            type="button"
            onClick={() => {
              closeMore();
              onSignOut();
            }}
            className={`inline-flex min-h-10 items-center gap-2 rounded-xl border border-line px-3 text-sm text-ink hover:bg-hover ${focusRing}`}
          >
            <LogOut size={15} aria-hidden /> Sign out
          </button>
        </div>
      </Dialog>
    </>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const queryClient = useQueryClient();
  const me = useIdentity();
  const unauthenticated = me.error instanceof ApiError && me.error.status === 401;
  // Nothing to bootstrap into from the desktop app: it holds a key for an
  // instance that, by definition, someone already set up in a browser.
  const bootstrap = useBootstrapStatus(!IS_DESKTOP);

  useEffect(() => {
    if (!unauthenticated) return;
    if (IS_DESKTOP) {
      // The key was revoked, expired, or the instance was reinstalled. Only
      // the shell can fix that, because only the shell holds the key.
      goToConnect("unauthorized");
      return;
    }
    if (!bootstrap.data) return;
    router.replace(bootstrap.data.needs_bootstrap ? "/setup" : "/login");
  }, [unauthenticated, bootstrap.data, router]);

  const logout = useMutation({
    mutationFn: async () => {
      // A key cannot log out — there is no session. Disconnecting is the
      // equivalent act, and it is the shell that forgets the credential.
      if (IS_DESKTOP) return;
      await api<void>("/api/v1/auth/logout", { method: "POST" });
    },
    onSettled: () => {
      queryClient.clear();
      if (IS_DESKTOP) {
        goToConnect("disconnect");
        return;
      }
      router.replace("/login");
    },
  });

  if (me.isPending || unauthenticated) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label={unauthenticated ? "Redirecting…" : "Loading…"} />
      </div>
    );
  }
  if (me.error || !me.data) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-2 text-sm text-dim">
        <p>The API is unreachable.</p>
        <button
          className={`rounded-md text-accent-strong underline ${focusRing}`}
          onClick={() => me.refetch()}
        >
          Retry
        </button>
      </div>
    );
  }

  const workspace = me.data.memberships[0];
  if (!workspace) {
    // Reachable two ways: an account that was never added to a workspace, and
    // an owner who has just deleted their last one. Creating a workspace needs
    // only an authenticated user and makes the creator its owner, so the
    // useful thing to offer is a new workspace rather than a note telling
    // someone to go find an admin. A brand-new membership starts onboarding
    // as `pending`, so the guided tour opens on arrival.
    return <NoWorkspace onSignOut={() => logout.mutate()} />;
  }

  const user = me.data.user;

  return (
    <WorkspaceProvider user={user} workspace={workspace}>
      {/* Inside the workspace provider and outside every page: the first-run
          introduction has to know who this is, and must survive navigation
          between the pages it sends people to. */}
      <OnboardingProvider>
      <div className="flex min-h-screen">
        {/* Desktop sidebar (lg+) */}
        <aside className="fixed inset-y-0 left-0 z-20 hidden w-[260px] flex-col border-r border-line bg-surface lg:flex">
          <div className="px-5 pb-3 pt-5">
            <Link href="/home" className={`inline-flex rounded-lg ${focusRing}`} aria-label="Jhin home">
              <Wordmark />
            </Link>
            <p className="mt-2 truncate text-[13px] text-dim" title={workspace.workspace_name}>
              {workspace.workspace_name}
            </p>
          </div>
          <SidebarNav pathname={pathname} />
          <div className="border-t border-line px-4 py-3">
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">{user.display_name}</p>
                <p className="truncate text-xs capitalize text-faint">{workspace.role}</p>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <ThemeToggle />
                <button
                  type="button"
                  title="Sign out"
                  aria-label="Sign out"
                  onClick={() => logout.mutate()}
                  className={`inline-flex h-10 w-10 items-center justify-center rounded-xl text-dim hover:bg-hover hover:text-ink ${focusRing}`}
                >
                  <LogOut size={16} aria-hidden />
                </button>
              </div>
            </div>
          </div>
        </aside>

        {/* Tablet icon rail (md to lg) */}
        <aside className="fixed inset-y-0 left-0 z-20 hidden w-[72px] flex-col items-stretch border-r border-line bg-surface md:flex lg:hidden">
          <div className="flex justify-center py-4">
            <Link href="/home" className={`rounded-lg ${focusRing}`} aria-label="Jhin home">
              <Wordmark className="[&>span]:sr-only" />
            </Link>
          </div>
          <SidebarNav pathname={pathname} rail />
          <div className="flex flex-col items-center gap-1 border-t border-line py-3">
            <ThemeToggle />
            <button
              type="button"
              title="Sign out"
              aria-label="Sign out"
              onClick={() => logout.mutate()}
              className={`inline-flex h-10 w-10 items-center justify-center rounded-xl text-dim hover:bg-hover hover:text-ink ${focusRing}`}
            >
              <LogOut size={16} aria-hidden />
            </button>
          </div>
        </aside>

        <div className="flex min-h-screen min-w-0 flex-1 flex-col md:ml-[72px] lg:ml-[260px]">
          <MobileBars pathname={pathname} onSignOut={() => logout.mutate()} />
          <main id="main" className="flex min-w-0 flex-1 flex-col pb-16 md:pb-0">
            {children}
          </main>
        </div>
      </div>
      </OnboardingProvider>
    </WorkspaceProvider>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
  compact = false,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  eyebrow?: string;
  compact?: boolean;
}) {
  return (
    // top-14 below md: the mobile top bar (h-14, z-30) shares this scroll
    // context, and a top-0 header would pin invisibly underneath it.
    <header className="sticky top-14 z-10 border-b border-line bg-bg/85 backdrop-blur md:top-0">
      <div
        className={`mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-x-6 gap-y-3 px-5 md:px-8 ${
          compact ? "py-3" : "py-5"
        }`}
      >
        <div className="min-w-0">
          {eyebrow ? <p className="eyebrow mb-1">{eyebrow}</p> : null}
          <h1
            className={`font-display font-semibold tracking-tight ${compact ? "text-lg" : "text-2xl"}`}
          >
            {title}
          </h1>
          {description ? <p className="mt-1 text-sm text-dim">{description}</p> : null}
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    </header>
  );
}

/** Constrained page body shared by every page: one source of truth for
 * gutters and centering. `wide` for dense tables, `narrow` for reading
 * columns like feeds and inboxes. */
export function PageBody({
  children,
  className = "",
  wide = false,
  narrow = false,
}: {
  children: React.ReactNode;
  className?: string;
  wide?: boolean;
  narrow?: boolean;
}) {
  const maxWidth = narrow ? "max-w-3xl" : wide ? "max-w-7xl" : "max-w-6xl";
  return (
    <div className={`mx-auto w-full px-5 py-6 md:px-8 md:py-8 ${maxWidth} ${className}`}>
      {children}
    </div>
  );
}


/** Shown to a signed-in account with no workspace: create one, or sign out. */
function NoWorkspace({ onSignOut }: { onSignOut: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api<{ id: string }>("/api/v1/workspaces", {
        method: "POST",
        body: {
          name: name.trim(),
          // The browser knows the viewer's zone; the workspace uses it for
          // agent "today" reasoning and scheduled work.
          default_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        },
      }),
    // Re-reading identity swaps this screen for the app itself, where the
    // onboarding tour opens on the fresh membership.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["identity"] }),
  });

  return (
    <div
      data-testid="no-workspace"
      className="flex min-h-screen flex-col items-center justify-center px-6"
    >
      <div className="w-full max-w-sm space-y-5 text-center">
        <Wordmark className="justify-center" />
        <div className="space-y-1.5">
          <h1 className="font-display text-xl font-semibold text-ink">Create your workspace</h1>
          <p className="text-sm text-dim">
            A workspace holds your AI teammates, their chats, and everything they connect to.
            You will own this one.
          </p>
        </div>
        <form
          className="space-y-3 text-left"
          onSubmit={(event) => {
            event.preventDefault();
            if (name.trim()) create.mutate();
          }}
        >
          <Field label="Workspace name">
            <Input
              autoFocus
              required
              maxLength={200}
              value={name}
              placeholder="Acme HQ"
              disabled={create.isPending}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <ErrorNote
            message={
              create.error instanceof ApiError
                ? create.error.detail
                : create.error
                  ? "The workspace could not be created."
                  : null
            }
          />
          <Button
            type="submit"
            variant="primary"
            className="w-full justify-center"
            disabled={create.isPending || !name.trim()}
          >
            {create.isPending ? "Creating…" : "Create workspace"}
          </Button>
        </form>
        <p className="text-xs text-faint">
          Expecting an invitation instead? Ask an admin to invite this account, or{" "}
          <button
            className={`rounded-md text-accent-strong underline ${focusRing}`}
            onClick={onSignOut}
          >
            sign out
          </button>
          .
        </p>
      </div>
    </div>
  );
}
