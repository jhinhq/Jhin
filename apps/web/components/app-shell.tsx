"use client";

/** Authenticated application shell: sidebar navigation (plan 17.2) + guard.
 * Unauthenticated visitors are redirected to /login (or /setup on first run).
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Building2,
  CheckSquare,
  ClipboardList,
  Cpu,
  LayoutDashboard,
  ListTodo,
  LogOut,
  Plug,
  ScrollText,
  Settings,
  Zap,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useBootstrapStatus, useMe, usePendingApprovalCount } from "@/lib/hooks";
import { useWorkspace, WorkspaceProvider } from "@/lib/workspace-context";

const NAV = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/organization", label: "Organization", icon: Building2 },
  { href: "/tasks", label: "Tasks", icon: ListTodo },
  { href: "/runs", label: "Runs", icon: Activity },
  { href: "/approvals", label: "Approvals", icon: CheckSquare },
  { href: "/connectors", label: "Connectors", icon: Plug },
  { href: "/triggers", label: "Triggers", icon: Zap },
  { href: "/models", label: "Models", icon: Cpu },
  { href: "/audit", label: "Audit", icon: ScrollText },
  { href: "/settings", label: "Settings", icon: Settings },
] as const;

function NavLink({
  href,
  label,
  icon: Icon,
  active,
  badge,
}: {
  href: string;
  label: string;
  icon: typeof ClipboardList;
  active: boolean;
  badge?: number;
}) {
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={`flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px] transition-colors ${
        active
          ? "bg-accent-soft font-medium text-accent-strong"
          : "text-dim hover:bg-hover hover:text-ink"
      }`}
    >
      <Icon size={15} strokeWidth={1.8} />
      <span className="flex-1">{label}</span>
      {badge !== undefined && badge > 0 ? (
        <span className="rounded-full bg-warn/15 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-warn">
          {badge}
        </span>
      ) : null}
    </Link>
  );
}

/** Sidebar nav; lives inside WorkspaceProvider so the approvals badge can poll. */
function SidebarNav({ pathname }: { pathname: string }) {
  const { workspace } = useWorkspace();
  const pendingApprovals = usePendingApprovalCount(workspace.workspace_id);
  return (
    <nav className="flex-1 space-y-0.5 overflow-y-auto px-2 py-2">
      {NAV.map((item) => (
        <NavLink
          key={item.href}
          {...item}
          badge={item.href === "/approvals" ? pendingApprovals.data : undefined}
          active={item.href === "/" ? pathname === "/" : pathname.startsWith(item.href)}
        />
      ))}
    </nav>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const queryClient = useQueryClient();
  const me = useMe();
  const unauthenticated = me.error instanceof ApiError && me.error.status === 401;
  const bootstrap = useBootstrapStatus();

  useEffect(() => {
    if (!unauthenticated || !bootstrap.data) return;
    router.replace(bootstrap.data.needs_bootstrap ? "/setup" : "/login");
  }, [unauthenticated, bootstrap.data, router]);

  const logout = useMutation({
    mutationFn: () => api<void>("/api/v1/auth/logout", { method: "POST" }),
    onSettled: () => {
      queryClient.clear();
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
        <button className="text-accent-strong underline" onClick={() => me.refetch()}>
          Retry
        </button>
      </div>
    );
  }

  const workspace = me.data.memberships[0];
  if (!workspace) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-dim">
        Your account has no workspace membership. Ask a workspace admin to add you.
      </div>
    );
  }

  return (
    <WorkspaceProvider user={me.data.user} workspace={workspace}>
      <div className="flex min-h-screen">
        <aside className="fixed inset-y-0 left-0 flex w-56 flex-col border-r border-line bg-surface">
          <div className="flex items-center gap-2.5 px-4 py-4">
            <div className="flex h-7 w-7 items-center justify-center rounded-lg border border-accent/40 bg-accent-soft text-sm font-bold text-accent-strong">
              J
            </div>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold leading-tight">Jhin</p>
              <p className="truncate text-[11px] text-faint">{workspace.workspace_name}</p>
            </div>
          </div>
          <SidebarNav pathname={pathname} />
          <div className="border-t border-line px-3 py-3">
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-[13px] font-medium">{me.data.user.display_name}</p>
                <p className="truncate text-[11px] capitalize text-faint">{workspace.role}</p>
              </div>
              <button
                title="Sign out"
                aria-label="Sign out"
                onClick={() => logout.mutate()}
                className="rounded-md p-1.5 text-dim hover:bg-hover hover:text-ink"
              >
                <LogOut size={15} />
              </button>
            </div>
          </div>
        </aside>
        <main className="ml-56 min-h-screen flex-1">{children}</main>
      </div>
    </WorkspaceProvider>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="sticky top-0 z-10 flex items-center justify-between gap-4 border-b border-line bg-bg/90 px-8 py-4 backdrop-blur">
      <div>
        <h1 className="text-base font-semibold tracking-tight">{title}</h1>
        {description ? <p className="mt-0.5 text-[13px] text-dim">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}
