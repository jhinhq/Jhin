/**
 * The guided introduction, as data.
 *
 * Two things decide what a newcomer sees. **Role**: someone who cannot
 * configure a model provider must never be handed a step that asks them to,
 * so an invited member gets a shorter, orientation-only version. **Real
 * state**: every step that can be finished reports whether it already is, read
 * from the same `useSetupStatus` the Home checklist uses, so nobody is asked
 * to do something the workspace has already got.
 *
 * The order is a dependency chain, not a preference: an agent with no model
 * cannot run, and a chat with no agent has nobody to answer. Steps whose
 * prerequisite is missing say so instead of leading someone into a dead end.
 *
 * Pure data on purpose — no hooks, no JSX — so the whole flow is testable
 * without rendering anything.
 */

import {
  BellRing,
  BookOpen,
  Bot,
  Building2,
  Cpu,
  MessageSquare,
  Radio,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";

export type OnboardingStepId =
  | "welcome"
  | "model"
  | "agent"
  | "chat"
  | "apps"
  | "teamwork"
  | "explore";

export interface TourHighlight {
  icon: LucideIcon;
  title: string;
  body: string;
  href: string;
}

export interface OnboardingStep {
  id: OnboardingStepId;
  /** Short label for the step strip. */
  navLabel: string;
  title: string;
  body: string;
  /**
   * `true`/`false` for a step you can finish, `null` for one that only
   * introduces something. A `null` step is never counted as outstanding.
   */
  done: boolean | null;
  /** Nice to have, not required before the workspace works. */
  optional?: boolean;
  action?: { label: string; href: string };
  /** Why the action cannot be taken yet, in plain words. */
  blocked?: string;
  highlights?: TourHighlight[];
}

export interface OnboardingFacts {
  workspaceName: string;
  /** Admin or owner: may set up providers, apps, and agents. */
  canConfigure: boolean;
  /** Member or above: may start chats. A viewer only watches. */
  canChat: boolean;
  hasModel: boolean;
  hasAgents: boolean;
  hasChats: boolean;
  hasApps: boolean;
}

const COMPANY: TourHighlight = {
  icon: Building2,
  title: "Company",
  body: "Teams, managers, and an org chart you rearrange by dragging people around.",
  href: "/company",
};

const ACTIVITY: TourHighlight = {
  icon: Radio,
  title: "Activity",
  body: "A live feed of what everyone is doing, including the work they hand to each other.",
  href: "/activity",
};

const ATTENTION: TourHighlight = {
  icon: BellRing,
  title: "Attention",
  body: "Anything risky stops here and waits for your yes or no. Nothing big happens behind your back.",
  href: "/attention",
};

const AUTOMATIONS: TourHighlight = {
  icon: Zap,
  title: "Automations",
  body: "Start work on its own when something happens — a new issue, a merged change.",
  href: "/automations",
};

const SKILLS: TourHighlight = {
  icon: BookOpen,
  title: "Skills",
  body: "A library of instructions your teammates can read, with more installable from GitHub.",
  href: "/skills",
};

const SPEND: TourHighlight = {
  icon: Cpu,
  title: "Models and spend",
  body: "Set a monthly budget, and see what every piece of work actually cost.",
  href: "/models",
};

const ADVANCED: TourHighlight = {
  icon: Wrench,
  title: "Advanced",
  body: "The detail when you want it: every run, approvals, review rules, the audit log, and API keys.",
  href: "/advanced",
};

const AGENTS_DIRECTORY: TourHighlight = {
  icon: Bot,
  title: "Your teammates",
  body: "Who works here, what each one is good at, and how to reach them.",
  href: "/agents",
};

const CHATS: TourHighlight = {
  icon: MessageSquare,
  title: "Chats",
  body: "Every conversation you have had, and the work that came out of it.",
  href: "/chats",
};

function welcomeStep(facts: OnboardingFacts): OnboardingStep {
  return {
    id: "welcome",
    navLabel: "Welcome",
    title: `Welcome to ${facts.workspaceName}`,
    body: facts.canConfigure
      ? "Jhin is a company of AI teammates. You hire them, give each one a job, and then talk to them like colleagues — they do the work, ask each other for help, and check with you before anything risky. This takes a few minutes, and you can leave at any point."
      : "Jhin is a company of AI teammates. You ask for something in plain language, and the right one picks it up — doing the work, asking a colleague when they need to, and coming back to you when a person has to decide. Here is where everything lives.",
    done: null,
  };
}

function modelStep(facts: OnboardingFacts): OnboardingStep {
  return {
    id: "model",
    navLabel: "Model",
    title: "Connect a model provider",
    body: facts.hasModel
      ? "Done — your teammates have a model to think with. You can add more, or change which one is the default, whenever you like."
      : "Your teammates think with an AI model, so this comes first: nothing runs without it. Bring your own key from OpenAI, Anthropic, or any compatible endpoint. You keep the account, and you set the budget.",
    done: facts.hasModel,
    action: { label: facts.hasModel ? "Review your models" : "Set up a model", href: "/models" },
  };
}

function agentStep(facts: OnboardingFacts): OnboardingStep {
  return {
    id: "agent",
    navLabel: "First teammate",
    title: "Create your first teammate",
    body: facts.hasAgents
      ? "You have someone on the team already. Add more whenever there is a job that needs its own pair of hands."
      : "Give them a name, a face, and a job — a researcher, an engineer, a writer. Pick what they are allowed to do from a short list of presets. It takes about a minute, and nothing here is permanent.",
    done: facts.hasAgents,
    action: {
      label: facts.hasAgents ? "See your teammates" : "Create a teammate",
      href: facts.hasAgents ? "/agents" : "/agents/new",
    },
    blocked: facts.hasModel
      ? undefined
      : "Connect a model provider first — a teammate with no model cannot do anything.",
  };
}

function chatStep(facts: OnboardingFacts): OnboardingStep {
  const blocked = facts.hasAgents
    ? undefined
    : facts.canConfigure
      ? "Create a teammate first — there is nobody to talk to yet."
      : "Nobody has been hired yet. An admin sets up the first teammate.";
  return {
    id: "chat",
    navLabel: "First chat",
    title: facts.canChat ? "Start a conversation" : "How the work happens",
    body: facts.hasChats
      ? "Your first chat is under way. Every reply is real work: you can watch each step, steer it while it runs, or stop it outright."
      : facts.canChat
        ? "Chat is where work starts. Ask for something the way you would ask a colleague. Every reply is real work you can watch step by step, steer while it runs, or stop outright."
        : "People ask for work in a chat, in plain language. You can read along: every reply is real work, shown step by step, with the result at the end.",
    done: facts.hasChats,
    action: facts.canChat ? { label: "Open chats", href: "/chats" } : undefined,
    blocked: facts.canChat ? blocked : undefined,
    highlights: facts.canChat ? undefined : [CHATS, AGENTS_DIRECTORY],
  };
}

function appsStep(facts: OnboardingFacts): OnboardingStep {
  return {
    id: "apps",
    navLabel: "Apps",
    title: "Let them work in your real tools",
    body: facts.hasApps
      ? "An app is connected, so your teammates can act there rather than only talk about it. You decide which tools each one may use."
      : "Connect GitHub, Linear, or any MCP server, and your teammates can read an issue, open a pull request, or file a ticket — in your systems, with permissions you choose. Skip this if you would rather come back to it.",
    done: facts.hasApps,
    optional: true,
    action: { label: facts.hasApps ? "Manage apps" : "Browse apps", href: "/apps" },
  };
}

function teamworkStep(facts: OnboardingFacts): OnboardingStep {
  return {
    id: "teamwork",
    navLabel: "Teamwork",
    title: "How work moves around",
    body: facts.canConfigure
      ? "This is not one assistant. Teammates sit in teams, pass work to each other when someone else knows better, and stop for a person whenever the next move is risky."
      : "Teammates sit in teams and pass work to each other when a colleague knows better. When the next move is risky, they stop and wait for a person.",
    done: null,
    highlights: [COMPANY, ACTIVITY, ATTENTION],
  };
}

function exploreStep(facts: OnboardingFacts): OnboardingStep {
  return {
    id: "explore",
    navLabel: "What else",
    title: "What else is here",
    body: "Nothing below needs doing today. It is here so you know where to look when you want it.",
    done: null,
    highlights: facts.canConfigure
      ? [AUTOMATIONS, SKILLS, SPEND, ADVANCED]
      : [AUTOMATIONS, SKILLS, AGENTS_DIRECTORY],
  };
}

/**
 * The steps this person should see, in the order their prerequisites allow.
 *
 * An admin gets the setup chain (model → teammate → chat → apps) followed by
 * the tour. Everyone else gets the tour alone: no step they are not allowed to
 * finish ever appears.
 */
export function buildOnboardingSteps(facts: OnboardingFacts): OnboardingStep[] {
  if (!facts.canConfigure) {
    return [welcomeStep(facts), chatStep(facts), teamworkStep(facts), exploreStep(facts)];
  }
  return [
    welcomeStep(facts),
    modelStep(facts),
    agentStep(facts),
    chatStep(facts),
    appsStep(facts),
    teamworkStep(facts),
    exploreStep(facts),
  ];
}

/**
 * Where to open the tour.
 *
 * Somebody who has never seen it starts at the beginning, because the first
 * screen is the part that explains what any of this is. Somebody coming back
 * resumes at the first thing still outstanding, so returning from setting up a
 * model lands on creating a teammate rather than on a page of ticks.
 */
export function startingStepIndex(steps: OnboardingStep[], firstTime: boolean): number {
  if (firstTime) return 0;
  const next = steps.findIndex((step) => step.done === false && !step.optional);
  if (next >= 0) return next;
  const optional = steps.findIndex((step) => step.done === false);
  return optional >= 0 ? optional : 0;
}

/** Steps that can be finished, and how many already are. */
export function stepProgress(steps: OnboardingStep[]): { done: number; total: number } {
  const finishable = steps.filter((step) => step.done !== null);
  return {
    done: finishable.filter((step) => step.done).length,
    total: finishable.length,
  };
}
