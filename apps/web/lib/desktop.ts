/**
 * Whether this bundle was built for the desktop shell (apps/desktop).
 *
 * Inlined at build time from `NEXT_PUBLIC_JHIN_DESKTOP`, so the browser build
 * evaluates this to a constant `false` and can never take a desktop-only path
 * at runtime.
 *
 * The difference that matters: the desktop app authenticates with an API key
 * held by the shell, not a session cookie. There is nothing to sign in or out
 * of, so `/login` does not exist and the shell's own connect screen — served
 * outside this bundle, which is why these are full page loads and not
 * `router.push` — takes its place.
 */
export const IS_DESKTOP = process.env.NEXT_PUBLIC_JHIN_DESKTOP === "1";

/** The shell's connect screen. Not a Next route: only the desktop shell serves it. */
export const CONNECT_PATH = "/connect";

export function goToConnect(reason?: "unauthorized" | "disconnect"): void {
  window.location.assign(reason ? `${CONNECT_PATH}?reason=${reason}` : CONNECT_PATH);
}
