/**
 * Reading a dynamic route segment in a way the desktop build can trust.
 *
 * The desktop shell ships this app as a static export, which cannot render a
 * page per id. Each dynamic route instead pre-renders one placeholder page
 * (`placeholderParams` below) and the shell serves that same file for every
 * id under the route. So the params baked into the payload are the
 * placeholder — `useParams()` would report `_` no matter which chat the user
 * opened. The pathname is the real URL in both builds, so that is what these
 * read.
 *
 * No React here: `useSegmentAfter` in `lib/use-route-segment.ts` is the hook,
 * and this half stays pure so it can be tested directly and imported from the
 * server components that call `generateStaticParams`.
 */

/** The id every dynamic route is pre-rendered under in the static export. */
export const PLACEHOLDER_PARAM = "_";

/**
 * The path segment directly after `prefix`, or `""` when there isn't one.
 *
 * `""` is returned for the placeholder too: a page rendered at the literal
 * placeholder URL has no real id to load, and every caller already handles
 * the empty case because a malformed URL could always produce it.
 */
export function segmentAfter(pathname: string | null, prefix: string): string {
  const parts = (pathname ?? "").split("/").filter(Boolean);
  const at = parts.indexOf(prefix);
  const value = at === -1 ? undefined : parts[at + 1];
  if (!value || value === PLACEHOLDER_PARAM) return "";
  try {
    return decodeURIComponent(value);
  } catch {
    // A malformed escape is a bad URL, not a crash.
    return "";
  }
}

/** What a dynamic route's `page.tsx` returns so static export has one page to build. */
export function placeholderParams<K extends string>(key: K): Array<Record<K, string>> {
  return [{ [key]: PLACEHOLDER_PARAM } as Record<K, string>];
}
