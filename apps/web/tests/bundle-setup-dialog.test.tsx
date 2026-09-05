/** The bundle setup dialog: every step shown, pre-filled when unambiguous,
 * a dry run before Review's primary button posts the same body. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BundleSetupDialog } from "@/components/org/bundle-setup-dialog";
import type { BundleApplyOut, BundleStatusOut, ConnectionInfo } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function connection(
  overrides: Partial<ConnectionInfo> & Pick<ConnectionInfo, "id" | "connector_type" | "name">,
): ConnectionInfo {
  return {
    auth_type: "pat",
    status: "active",
    public_id: `pub-${overrides.id}`,
    config_json: {},
    created_by_user_id: null,
    created_at: "2026-09-01T00:00:00Z",
    last_verified_at: null,
    last_error: null,
    webhook_secret_configured: false,
    ...overrides,
  };
}

const github = connection({ id: "gh-1", connector_type: "github", name: "GitHub" });
const otherGithub = connection({ id: "gh-2", connector_type: "github", name: "Other GitHub" });
const sandbox = connection({
  id: "cli-1",
  connector_type: "cli",
  name: "Existing sandbox",
  auth_type: "none",
  config_json: { git_connection_id: "gh-1", allowed_repositories: ["octo/alpha"] },
});

const CODE_EDITING: BundleStatusOut = {
  id: "code-editing",
  label: "Code editing",
  summary: "Write code",
  description: "",
  tools: [
    { name: "cli.repository.checkout", capability: "cli.repository.checkout", scope: { repository: "*" } },
    { name: "cli.repository.push", capability: "cli.repository.push", scope: { repository: "*", branch: "agent/*" } },
    { name: "github.repository.read", capability: "github.repository.read", scope: { repository: "*" } },
    { name: "github.pull_request.create", capability: "github.pull_request.create", scope: { repository: "*", base: "*" } },
  ],
  rules: [{ capability: "cli.repository.push", risk: null, action: "approval" }],
  not_included: ["pushing to main"],
  readiness: { state: "needs", needs: [{ kind: "create_sandbox", connector_type: "cli", choices: [], detail: "" }], missing_tools: [] },
  state: "off",
  granted_capabilities: [],
  missing_capabilities: ["cli.repository.checkout"],
  problems: [],
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
}

function preview(body: Record<string, unknown>, warnings: string[] = []): BundleApplyOut {
  return {
    bundle_id: "code-editing",
    dry_run: Boolean(body.dry_run),
    created_connection: null,
    grants_created: [
      {
        id: "00000000-0000-0000-0000-000000000000",
        agent_id: "agent-1",
        capability: "cli.repository.push",
        scope_json: { connection_id: "cli-1", repository: "*", branch: "agent/*" },
        effect: "allow",
        created_at: "2026-09-01T00:00:00Z",
        problems: [],
        connection_name: "Existing sandbox",
      },
    ],
    grants_existing: [],
    rules_added: [{ capability: "cli.repository.push", risk: null, action: "approval" }],
    rules_kept: [],
    callable_tools: ["cli.repository.push"],
    needs: [],
    warnings,
  };
}

function renderDialog(
  connections: ConnectionInfo[],
  {
    initial,
    respond,
  }: {
    initial?: { connectionId?: string };
    respond?: (body: Record<string, unknown>) => Response;
  } = {},
) {
  const bodies: Record<string, unknown>[] = [];
  const onDone = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/api/v1/connectors")) return json([]);
      if (path.endsWith("/agents/agent-1/bundles/code-editing") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        bodies.push(body);
        return respond ? respond(body) : json(preview(body));
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${path}`);
    }),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <WorkspaceProvider
        user={{ id: "user-1", email: "owner@example.com", display_name: "Owner", created_at: "2026-08-18T00:00:00Z" }}
        workspace={{ workspace_id: "workspace-1", workspace_name: "Acme", workspace_slug: "acme", role: "owner" }}
      >
        <BundleSetupDialog
          agent={{ id: "agent-1", name: "Senior SWE" }}
          bundle={CODE_EDITING}
          connections={connections}
          initial={initial}
          onDone={onDone}
          onClose={vi.fn()}
        />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
  return { bodies, onDone };
}

function next() {
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
}

describe("BundleSetupDialog", () => {
  it("shows every step, pre-filled, and posts a sandbox to create", async () => {
    const { bodies, onDone } = renderDialog([github]);
    expect(screen.getByRole("heading", { name: "Turn on Code editing for Senior SWE" })).toBeDefined();
    const steps = screen.getByRole("list", { name: "Setup steps" });
    expect(within(steps).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "GitHub",
      "Sandbox",
      "Repositories",
      "Review",
    ]);
    expect((screen.getByLabelText("Connection") as HTMLSelectElement).value).toBe("gh-1");
    next();

    expect(screen.getByTestId("bundle-step-sandbox")).toBeDefined();
    expect(screen.getByText(/None uses this GitHub connection yet/)).toBeDefined();
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Sandbox for GitHub");
    expect(screen.queryByLabelText("Use an existing sandbox")).toBeNull();
    fireEvent.click(screen.getByLabelText(/Only these/));
    fireEvent.change(screen.getByLabelText("Allowed repositories"), { target: { value: "not a repo" } });
    expect(screen.getByRole("alert").textContent).toBe("Use owner/name, for example octo/widgets");
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Allowed repositories"), { target: { value: "octo/alpha\nocto/beta" } });
    next();

    expect(screen.getByTestId("bundle-step-repositories")).toBeDefined();
    fireEvent.click(screen.getByLabelText("Only these"));
    fireEvent.change(screen.getByLabelText("Repositories"), { target: { value: "octo/gamma" } });
    expect(screen.getByRole("alert").textContent).toBe(
      "Sandbox for GitHub does not allow octo/gamma. Add it to the sandbox's allowed repositories under Apps first.",
    );
    fireEvent.change(screen.getByLabelText("Repositories"), { target: { value: "octo/alpha" } });
    next();

    expect(screen.getByRole("heading", { name: "What Senior SWE will be able to do" })).toBeDefined();
    expect(screen.getByText(/Not included: pushing to main/)).toBeDefined();
    expect(screen.getByText(/Will create: CLI Sandbox connection/).textContent).toContain(
      "‘Sandbox for GitHub’ (git credential: GitHub; repositories: octo/alpha, octo/beta)",
    );
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({
      connections: { github: "gh-1" },
      repositories: ["octo/alpha"],
      base: "*",
      dry_run: true,
      sandbox: { name: "Sandbox for GitHub", git_connection_id: "gh-1", allowed_repositories: ["octo/alpha", "octo/beta"] },
    });
    expect(await screen.findByText("Show the 1 grants and 1 rules this writes")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "Turn on Code editing" }));
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).toEqual({ ...bodies[0], dry_run: false });
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
  });

  it("uses an existing sandbox, shows warnings, and pre-chooses the linked connection", async () => {
    const { bodies } = renderDialog([github, otherGithub, sandbox], {
      initial: { connectionId: "gh-1" },
      respond: (body) => json(preview(body, ["An explicit deny on cli.repository.push for this agent still wins; remove it under Capability grants if the agent should use it."])),
    });
    // Two GitHub connections, but the drawer's link named one.
    expect((screen.getByLabelText("Connection") as HTMLSelectElement).value).toBe("gh-1");
    next();
    expect((screen.getByLabelText("Use an existing sandbox") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("Sandbox") as HTMLSelectElement).value).toBe("cli-1");
    next();
    expect(screen.getByLabelText("Every repository Existing sandbox allows")).toBeDefined();
    fireEvent.click(screen.getByText("Advanced: pull request base branch"));
    fireEvent.change(screen.getByLabelText(/Base branch pattern/), { target: { value: "main" } });
    next();

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({
      connections: { github: "gh-1", cli: "cli-1" },
      repositories: ["*"],
      base: "main",
      dry_run: true,
    });
    expect(await screen.findByTestId("bundle-warnings")).toBeDefined();
    expect(screen.getByText(/base main/)).toBeDefined();
    fireEvent.click(screen.getByText("Show the 1 grants and 1 rules this writes"));
    const rows = screen.getByTestId("bundle-review-rows");
    expect(within(rows).getAllByText(/cli\.repository\.push/).length).toBe(2); // the row and its rule
  });

  it("keeps the dialog open with the server's sentence when the write is refused", async () => {
    const { onDone } = renderDialog([github, sandbox], {
      respond: (body) =>
        body.dry_run ? json(preview(body)) : json({ detail: "'Existing sandbox' allows only: octo/alpha" }, 422),
    });
    next();
    next();
    next();
    await screen.findByText("Show the 1 grants and 1 rules this writes");
    fireEvent.click(screen.getByRole("button", { name: "Turn on Code editing" }));

    expect(await screen.findByRole("alert")).toBeDefined();
    expect(screen.getByRole("alert").textContent).toBe("'Existing sandbox' allows only: octo/alpha");
    expect(screen.getByTestId("bundle-setup-dialog")).toBeDefined();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("tells the admin to connect the app when no connection exists", () => {
    renderDialog([]);
    expect(screen.getByText(/No active GitHub connection\. Connect one on the Apps page first\./)).toBeDefined();
    expect(screen.getByRole("link", { name: "Open Apps" }).getAttribute("href")).toBe("/apps");
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
