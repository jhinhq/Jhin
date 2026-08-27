/**
 * Provisioning for the chat end-to-end specs, through the public API only.
 *
 * Every spec builds its own workspace, provider, model profile and agents, so
 * nothing here depends on seed data or on another spec having run first.
 *
 * The workspace comes with a user of its own, and that is not ceremony: the
 * app shell has no workspace switcher and always opens `memberships[0]` — the
 * account's *oldest* workspace (`ORDER BY workspace.created_at` in the API's
 * `list_memberships`). A workspace created a moment ago sorts last, so the
 * only account that can actually see it in the browser is one that has no
 * other. Inviting a fresh user and accepting the invitation is how that
 * account comes to exist.
 */

import type { APIRequestContext, PlaywrightWorkerArgs } from "@playwright/test";

type PlaywrightModule = PlaywrightWorkerArgs["playwright"];

export type Cookies = Awaited<ReturnType<APIRequestContext["storageState"]>>["cookies"];

/**
 * The dev seed account (apps/api/src/jhin_api/seed.py). The specs sign in as
 * it exactly once per test, to create a workspace and invite a stranger into
 * it; nothing they assert on lives in any workspace this account already has.
 */
const SEED_OWNER = { email: "owner@jhin.dev", password: "jhin-dev-password" };

/**
 * Where the *agent worker* reaches the fake provider. It is an in-stack
 * hostname on purpose: the model call is made from inside the compose network,
 * not from the browser or from this process, so the published host port
 * (127.0.0.1:8090) would be the wrong address to store on the provider row.
 */
const FAKE_PROVIDER_URL = process.env.JHIN_E2E_FAKE_PROVIDER_URL ?? "http://fake-provider:8080/v1";

/** The fake provider answers to any model name; this is the one it prices. */
export const MODEL_NAME = "fake-mini";

/** What `packages/models/.../testing/fake_openai.py` echoes back. Whichever
 * question comes back is the one the model actually saw last, which is the
 * whole oracle for prompt ordering. */
export function echoOf(question: string): string {
  return `[${MODEL_NAME}] Completed: ${question}`;
}

/** Thin JSON client over one signed-in session. Mirrors the browser's own
 * client (`apps/web/lib/api.ts`): same-origin `/api/*`, session cookie, and
 * the CSRF double-submit header on every write. */
export class JhinClient {
  private csrf: string | null = null;

  constructor(readonly ctx: APIRequestContext) {}

  async get<T>(path: string): Promise<T> {
    return this.send<T>("get", path);
  }

  async post<T>(path: string, body?: unknown): Promise<T> {
    return this.send<T>("post", path, body);
  }

  async put<T>(path: string, body: unknown): Promise<T> {
    return this.send<T>("put", path, body);
  }

  async dispose(): Promise<void> {
    await this.ctx.dispose();
  }

  async cookies(): Promise<Cookies> {
    return (await this.ctx.storageState()).cookies;
  }

  private async send<T>(
    method: "get" | "post" | "put",
    path: string,
    body?: unknown,
  ): Promise<T> {
    const headers: Record<string, string> = {};
    if (method !== "get") {
      const csrf = await this.csrfToken();
      if (csrf) headers["x-csrf-token"] = csrf;
    }
    const response = await this.ctx[method](path, { headers, data: body ?? undefined });
    if (!response.ok()) {
      throw new Error(
        `${method.toUpperCase()} ${path} → ${response.status()}: ${await response.text()}`,
      );
    }
    if (response.status() === 204) return undefined as T;
    return (await response.json()) as T;
  }

  /** Null before the first sign-in: logging in and accepting an invitation are
   * the two writes that *mint* the cookie, so they cannot present it. */
  private async csrfToken(): Promise<string | null> {
    this.csrf ??= (await this.cookies()).find((entry) => entry.name === "jhin_csrf")?.value ?? null;
    return this.csrf;
  }
}

export interface AgentRef {
  id: string;
  name: string;
}

export interface ConversationRef {
  id: string;
  title: string;
}

interface ConversationDetail {
  conversation: {
    id: string;
    title: string;
    active_task_id: string | null;
    active_task_state: string | null;
  };
  tasks: { id: string; state: string; title: string }[];
}

/** Everything a spec needs to talk to its own workspace. */
export class Workspace {
  constructor(
    readonly id: string,
    readonly name: string,
    readonly client: JhinClient,
    readonly modelProfileId: string,
    /** Credentials of the throwaway user, for the spec that drives the real
     * sign-in form rather than injecting cookies. */
    readonly signIn: { email: string; password: string },
  ) {}

  private path(suffix: string): string {
    return `/api/v1/workspaces/${this.id}${suffix}`;
  }

  /**
   * `grants` are capability → scope pairs (`{"system.echo": {}}`); without an
   * allow grant the agent may not call the tool at all.
   */
  async createAgent(
    name: string,
    options: { grants?: Record<string, Record<string, unknown>>; maxSteps?: number } = {},
  ): Promise<AgentRef> {
    const agent = await this.client.post<AgentRef>(this.path("/agents"), {
      name,
      role_title: "End-to-end tester",
      system_prompt: "You answer briefly, and use tools when instructed.",
      model_profile_id: this.modelProfileId,
      ...(options.maxSteps ? { max_steps: options.maxSteps } : {}),
    });
    for (const [capability, scope] of Object.entries(options.grants ?? {})) {
      await this.client.post(this.path(`/agents/${agent.id}/grants`), {
        capability,
        scope,
        effect: "allow",
      });
    }
    return agent;
  }

  /** Start a chat with its first turn already sent. The title is passed
   * explicitly because the API otherwise derives it from the message, and a
   * message carrying a tool script makes for an unreadable assertion. */
  async startConversation(agent: AgentRef, text: string, title: string): Promise<ConversationRef> {
    const detail = await this.client.post<ConversationDetail>(this.path("/conversations"), {
      agent_id: agent.id,
      title,
      text,
      client_turn_id: crypto.randomUUID(),
    });
    return { id: detail.conversation.id, title: detail.conversation.title };
  }

  async conversation(id: string): Promise<ConversationDetail> {
    return this.client.get<ConversationDetail>(this.path(`/conversations/${id}`));
  }

  /** Resolve once no task on the conversation is running any more. */
  async waitUntilIdle(id: string, timeoutMs = 120_000): Promise<ConversationDetail> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const detail = await this.conversation(id);
      const state = detail.conversation.active_task_state;
      if (state === null || ["completed", "failed", "cancelled"].includes(state)) return detail;
      if (Date.now() > deadline) {
        throw new Error(`conversation ${id} still ${state} after ${timeoutMs}ms`);
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
}

async function signIn(
  playwright: PlaywrightModule,
  baseURL: string,
  email: string,
  password: string,
): Promise<JhinClient> {
  const client = new JhinClient(await playwright.request.newContext({ baseURL }));
  await client.post("/api/v1/auth/login", { email, password });
  return client;
}

/**
 * Build a workspace nobody else is using, and return a client signed in as its
 * owner. `label` only shapes the names, so a stray workspace can be traced
 * back to the spec that made it.
 */
export async function provisionWorkspace(
  playwright: PlaywrightModule,
  baseURL: string,
  label: string,
): Promise<Workspace> {
  const tag = crypto.randomUUID().slice(0, 8);
  const name = `E2E ${label} ${tag}`.slice(0, 200);
  const credentials = {
    email: `e2e-${tag}@jhin.dev`,
    // Twelve characters minimum, and no overlap with the address: the account
    // password policy refuses a password containing the local part.
    password: `browser-spec-${crypto.randomUUID().slice(0, 8)}`,
  };

  const seed = await signIn(playwright, baseURL, SEED_OWNER.email, SEED_OWNER.password);
  let workspaceId: string;
  let inviteToken: string;
  try {
    workspaceId = (await seed.post<{ id: string }>("/api/v1/workspaces", { name })).id;
    inviteToken = (
      await seed.post<{ token: string }>(`/api/v1/workspaces/${workspaceId}/invitations`, {
        email: credentials.email,
        role: "owner",
      })
    ).token;
  } finally {
    await seed.dispose();
  }

  const client = new JhinClient(await playwright.request.newContext({ baseURL }));
  await client.post(`/api/v1/invitations/${inviteToken}/accept`, {
    display_name: "E2E Tester",
    password: credentials.password,
  });

  // A brand-new membership starts onboarding as `pending`, which opens the
  // first-run tour over everything on arrival. Dismissing it here keeps the
  // specs about chat rather than about the overlay.
  await client.put(`/api/v1/workspaces/${workspaceId}/onboarding`, { status: "dismissed" });

  const provider = await client.post<{ id: string }>(
    `/api/v1/workspaces/${workspaceId}/model-providers`,
    {
      type: "openai_compatible",
      display_name: "Fake provider (e2e)",
      base_url: FAKE_PROVIDER_URL,
      // Runs are refused outright when the provider has no credit, and the
      // refusal reads as a product failure rather than a setup one.
      credits_loaded_micros: 1_000_000_000,
    },
  );
  const profile = await client.post<{ id: string }>(
    `/api/v1/workspaces/${workspaceId}/model-profiles`,
    {
      provider_id: provider.id,
      model_name: MODEL_NAME,
      display_name: "Fake mini (e2e)",
      input_cost_micros_per_million: 150_000,
      output_cost_micros_per_million: 600_000,
    },
  );

  return new Workspace(workspaceId, name, client, profile.id, credentials);
}
