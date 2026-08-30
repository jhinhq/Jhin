/** Tiny wording helpers shared by the friendly-copy builders (grants in
 * components/company/agent-helpers.ts, automations in lib/triggers.ts). */

export function titleCase(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

export function humanizeSegment(segment: string): string {
  return segment.replace(/_/g, " ");
}

/** How an app's capability prefix reads to a person. */
export const APP_LABELS: Record<string, string> = {
  github: "GitHub",
  linear: "Linear",
  cli: "Command line",
  vercel: "Vercel",
  organization: "Company",
  system: "System",
  http: "HTTP",
  slack: "Slack",
};
