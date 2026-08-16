"use client";

/** Current user + active workspace, provided by the authenticated shell. */

import { createContext, useContext } from "react";
import type { MembershipOut, UserOut, WorkspaceRole } from "@/lib/types";

export interface WorkspaceContextValue {
  user: UserOut;
  workspace: MembershipOut;
  role: WorkspaceRole;
  /** True when the role grants at least the given level. */
  can: (required: WorkspaceRole) => boolean;
}

const ROLE_ORDER: Record<WorkspaceRole, number> = {
  viewer: 0,
  member: 1,
  admin: 2,
  owner: 3,
};

export function roleSatisfies(actual: WorkspaceRole, required: WorkspaceRole): boolean {
  return ROLE_ORDER[actual] >= ROLE_ORDER[required];
}

const Context = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({
  user,
  workspace,
  children,
}: {
  user: UserOut;
  workspace: MembershipOut;
  children: React.ReactNode;
}) {
  const value: WorkspaceContextValue = {
    user,
    workspace,
    role: workspace.role,
    can: (required) => roleSatisfies(workspace.role, required),
  };
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(Context);
  if (!value) throw new Error("useWorkspace must be used inside the authenticated shell");
  return value;
}
