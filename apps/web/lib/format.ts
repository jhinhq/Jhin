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
