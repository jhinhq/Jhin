/** Formatting helpers. */

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function shortId(id: string | null): string {
  return id ? id.slice(0, 8) : "—";
}

/** Micro-dollars → human dollars ($1 = 1,000,000 micros). */
export function formatCostMicros(micros: number): string {
  if (micros === 0) return "$0.00";
  const dollars = micros / 1_000_000;
  return dollars < 0.01 ? `$${dollars.toFixed(5)}` : `$${dollars.toFixed(2)}`;
}

export function formatTokens(count: number): string {
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(1)}k`;
  return String(count);
}

const RELATIVE_STEPS: readonly [Intl.RelativeTimeFormatUnit, number][] = [
  ["second", 60],
  ["minute", 60],
  ["hour", 24],
  ["day", 7],
  ["week", 4.35],
  ["month", 12],
  ["year", Number.POSITIVE_INFINITY],
];

/** "in 6 days" / "3 hours ago" — used where the exact instant matters less
 * than how far away it is (invitation expiry, last time a key was used). */
export function formatRelative(iso: string): string {
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  let delta = (new Date(iso).getTime() - Date.now()) / 1000;
  if (!Number.isFinite(delta)) return "—";
  for (const [unit, span] of RELATIVE_STEPS) {
    if (Math.abs(delta) < span) return formatter.format(Math.round(delta), unit);
    delta /= span;
  }
  return formatter.format(Math.round(delta), "year");
}
