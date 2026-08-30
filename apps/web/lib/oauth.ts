/**
 * Pure helpers for the OAuth connect flow (docs/architecture/oauth.md).
 * React-free and unit-tested.
 *
 * Two rules run through this file.
 *
 * Nothing here ever holds credential material. The device *user code* is a
 * display string that is worthless without the device code the server keeps;
 * the authorization URL carries a client id and a PKCE challenge, both public
 * by definition. There is no other token-shaped value in the connect flow the
 * browser is given, and none should be added.
 *
 * The stored return route is attacker-influenceable — it lives in
 * `sessionStorage`, which any script on this origin can write. It is
 * therefore validated as a same-origin path on the way *out*, not on the way
 * in, so a value planted before this code shipped can still never become an
 * off-site redirect.
 */

const RETURN_ROUTE_KEY = "jhin.oauth.return";

/** A path we are willing to send the browser back to: same-origin, rooted,
 * and unable to be read as a protocol-relative or backslash-smuggled URL. */
function isSafeReturnRoute(value: string): boolean {
  if (value.length === 0 || value.length > 2000) return false;
  if (!value.startsWith("/")) return false;
  // "//evil.example" and "/\\evil.example" are both off-site in a browser.
  if (value.startsWith("//") || value.startsWith("/\\")) return false;
  if (value.includes("\\")) return false;
  // Control characters and whitespace are how a URL gets split in two.
  return !/[\u0000-\u0020\u007f]/.test(value);
}

/**
 * Remember where the user was before the browser leaves for the provider.
 *
 * Every failure is swallowed: a private window with storage disabled must
 * cost somebody the return trip, never the connection.
 */
export function saveReturnRoute(route?: string): void {
  try {
    const value =
      route ??
      (typeof window === "undefined"
        ? ""
        : `${window.location.pathname}${window.location.search}`);
    if (!isSafeReturnRoute(value)) return;
    window.sessionStorage.setItem(RETURN_ROUTE_KEY, value);
  } catch {
    /* storage unavailable (private mode, blocked cookies) */
  }
}

/** Read and clear the stored route. Returns null unless it is a route this
 * origin can safely navigate to. */
export function consumeReturnRoute(): string | null {
  let raw: string | null = null;
  try {
    raw = window.sessionStorage.getItem(RETURN_ROUTE_KEY);
    window.sessionStorage.removeItem(RETURN_ROUTE_KEY);
  } catch {
    return null;
  }
  if (raw === null || !isSafeReturnRoute(raw)) return null;
  return raw;
}

/**
 * The device code as a person should read it.
 *
 * GitHub already sends `WDJB-MJHT`; other servers send eight bare characters.
 * Idempotent on purpose — a code that arrives grouped is left exactly as it
 * came, so this can be applied without knowing which server issued it.
 */
export function formatUserCode(code: string): string {
  const trimmed = code.trim();
  if (trimmed.includes("-") || trimmed.includes(" ")) return trimmed.toUpperCase();
  if (trimmed.length !== 8 || !/^[A-Za-z0-9]+$/.test(trimmed)) return trimmed.toUpperCase();
  return `${trimmed.slice(0, 4)}-${trimmed.slice(4)}`.toUpperCase();
}

/**
 * The scope list as a sentence, for the consent card.
 *
 * Scope strings are the authorization server's words, not ours, so they are
 * shown verbatim rather than "translated" into a guess. An empty list is
 * honest about being empty: the server asked for nothing specific.
 */
export function describeScopes(scopes: string[]): string {
  const cleaned = scopes.map((scope) => scope.trim()).filter(Boolean);
  if (cleaned.length === 0) return "the access this app needs by default";
  if (cleaned.length === 1) return cleaned[0];
  if (cleaned.length === 2) return `${cleaned[0]} and ${cleaned[1]}`;
  return `${cleaned.slice(0, -1).join(", ")}, and ${cleaned[cleaned.length - 1]}`;
}

/**
 * How long `slow_down` adds to the device-poll cadence, and permanently.
 *
 * RFC 8628 says a client MUST increase its interval by 5 seconds each time
 * the server says so. Quietly returning to the old rate afterwards is how a
 * client gets rate-limited out of the flow altogether, so the raise never
 * comes back down for the life of the authorization.
 */
export const SLOW_DOWN_STEP_MS = 5_000;

/**
 * The delay before the next device poll, or `false` when there is nothing
 * left to ask about.
 *
 * The server's own `interval` is a floor, never a ceiling: it may raise the
 * cadence mid-flow and the client must honour it, but it cannot talk the
 * client back down below the rate it started at or below the backoff already
 * earned.
 */
export function devicePollDelayMs(
  baseMs: number,
  status: string | undefined,
  serverIntervalSeconds: number | null | undefined,
  backoffMs: number,
): number | false {
  if (status === "connected" || status === "denied" || status === "expired") return false;
  const serverMs = Math.max(0, serverIntervalSeconds ?? 0) * 1000;
  return Math.max(baseMs, serverMs) + Math.max(0, backoffMs);
}

/** Seconds left until an ISO-8601 instant, floored at zero. */
export function secondsUntil(iso: string, now: number = Date.now()): number {
  const target = Date.parse(iso);
  if (Number.isNaN(target)) return 0;
  return Math.max(0, Math.floor((target - now) / 1000));
}

/** `m:ss` for the device-code countdown. */
export function formatCountdown(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(safe / 60);
  return `${minutes}:${String(safe % 60).padStart(2, "0")}`;
}

/**
 * What the callback's `?oauth_error=` means, in words a person can act on.
 *
 * The provider's own `error_description` never reaches the browser — it is
 * attacker-influenced text — so the vocabulary here is closed.
 */
export function oauthErrorMessage(code: string | null): string | null {
  if (code === "denied") {
    return "The permission request was declined, so nothing was connected. You can try again whenever you like.";
  }
  if (code === "failed") {
    return "That connection attempt could not be completed. Start again from the app you were connecting.";
  }
  return code ? "That connection attempt could not be completed. Start again from the app you were connecting." : null;
}

/**
 * POST a form to a third-party page the way a `<form>` would.
 *
 * GitHub's app-manifest flow takes a form POST from the browser and nothing
 * else — no redirect, no fetch — so this builds the form our API described,
 * submits it, and cleans up. It navigates the current tab on purpose: a popup
 * would be blocked in exactly the browsers this has to work in.
 */
export function postFormTo(url: string, fields: Record<string, string>): void {
  if (typeof document === "undefined") return;
  const form = document.createElement("form");
  form.method = "POST";
  form.action = url;
  form.style.display = "none";
  for (const [name, value] of Object.entries(fields)) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.append(input);
  }
  document.body.append(form);
  try {
    form.submit();
  } finally {
    form.remove();
  }
}

/**
 * Leave for the provider's consent page.
 *
 * The one place in the web app that hands the browser to a third party, and
 * it only ever takes a URL our own API built. A full-page navigation, never a
 * popup: popups are blocked without a gesture in several browsers, break on
 * mobile, and a top-level GET is what carries the `SameSite=Lax` session
 * cookie back to the callback with no cookie policy relaxed anywhere.
 */
export function navigateToProvider(url: string): void {
  window.location.assign(url);
}

/**
 * Auth schemes that end at a provider's consent screen rather than at a
 * pasted secret.
 *
 * The connector manifest is the authority on which of them a connector
 * actually offers — not the catalog's `auth_hint`, which the index gets wrong
 * far more often than right. A connector declaring none of these cannot sign
 * in at all, so there is nothing to ask the server about and the API-key form
 * is reached with no round trip at all.
 */
const SIGN_IN_SCHEMES = new Set(["oauth", "device_code", "device_flow", "device"]);

export function connectorSignsIn(connector: { auth_schemes: { type: string }[] }): boolean {
  return connector.auth_schemes.some((scheme) => SIGN_IN_SCHEMES.has(scheme.type));
}

/** The connections this workspace can no longer act with until somebody
 * signs in again. Tolerates an API that predates the `needs_reauth` status. */
export function needsReauth<T extends { status: string; needs_reauth?: boolean }>(
  connections: T[],
): T[] {
  return connections.filter(
    (connection) => connection.status === "needs_reauth" || connection.needs_reauth === true,
  );
}
