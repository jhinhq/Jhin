/** Just enough markdown, parsed into tokens the renderer turns into React
 * elements (never HTML). Two callers share it: the API reference, whose prose
 * comes from our own OpenAPI document, and the chat transcript, whose prose is
 * model output. The second caller is why link hrefs are validated here — model
 * text can be steered by anything the agent read (a fetched page, an MCP tool
 * result, a GitHub issue), so `[x](javascript:…)` is an input we must expect.
 *
 * Deliberately small: six syntax rules do not justify a markdown dependency,
 * and every dependency here is also a CSP and bundle-size decision. */

export type Inline =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "strong"; text: string }
  | { kind: "em"; text: string }
  | { kind: "link"; text: string; href: string };

export type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; inline: Inline[] }
  | { kind: "list"; ordered: boolean; items: Inline[][] }
  | { kind: "code"; text: string };

/* -------------------------------------------------------------------------
 * Link safety
 * ---------------------------------------------------------------------- */

/** The only schemes a link may carry. `javascript:` and `data:` are the
 * obvious exclusions; `vbscript:`, `file:` and app-handler schemes
 * (`ms-msdt:`, `steam:`, …) are excluded for the same reason — a click should
 * only ever be able to open a page or an email draft. */
const SAFE_SCHEMES = new Set(["http:", "https:", "mailto:"]);

/** Characters a browser throws away before it decides what scheme a URL has:
 * C0 controls and space, DEL, NBSP, and the Unicode line/paragraph
 * separators. `java\nscript:alert(1)` navigates exactly like
 * `javascript:alert(1)`, so the scheme has to be judged with them removed. */
const IGNORED_IN_SCHEME = /[\u0000-\u0020\u007f\u00a0\u2028\u2029]+/g;

const SCHEME = /^([a-zA-Z][a-zA-Z0-9+.\-]*):/;

/** The href to render, or `null` if this link must not be a link at all.
 *
 * Accepted: an absolute URL in one of `SAFE_SCHEMES`, or a reference that can
 * only point back at this app (`/path`, `./x`, `../x`, `#anchor`, `?q=1`).
 * Everything else — including scheme-relative `//evil.com`, bare `evil.com`,
 * and anything whose first colon is preceded by characters that are not a
 * legal scheme — is refused, and the caller shows the raw markdown as text. */
export function safeHref(raw: string): string | null {
  const href = raw.trim();
  if (!href) return null;
  // Judge the scheme on the string with the ignorable characters removed, but
  // hand back what the author actually wrote once it has passed.
  const probe = href.replace(IGNORED_IN_SCHEME, "");
  if (!probe) return null;
  if (probe.startsWith("//")) return null;
  const scheme = SCHEME.exec(probe);
  if (scheme) return SAFE_SCHEMES.has(`${scheme[1].toLowerCase()}:`) ? href : null;
  // No scheme. Allow only the shapes that are unambiguously relative, so that
  // an obfuscated scheme the regex above did not recognise cannot slip past as
  // "probably relative".
  if (/^[/?#]/.test(probe) || probe.startsWith("./") || probe.startsWith("../")) return href;
  return null;
}

/** True for a link that leaves the app, so the renderer can open it in a new
 * tab. Relative references stay in the current one. */
export function isExternalHref(href: string): boolean {
  return SCHEME.test(href.trim().replace(IGNORED_IN_SCHEME, ""));
}

/* -------------------------------------------------------------------------
 * Inline
 * ---------------------------------------------------------------------- */

const INLINE_PATTERN = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)|\*[^*\n]+\*)/g;

/** Inline markdown, tokenised. A link whose href fails `safeHref` degrades to
 * the literal source text — brackets, parens and all — so the reader can see
 * that something tried to be a link and what it pointed at. */
export function parseInline(text: string): Inline[] {
  const tokens: Inline[] = [];
  let cursor = 0;
  const push = (token: Inline) => {
    const previous = tokens[tokens.length - 1];
    if (token.kind === "text" && previous?.kind === "text") previous.text += token.text;
    else tokens.push(token);
  };
  for (const match of text.matchAll(INLINE_PATTERN)) {
    const index = match.index ?? 0;
    if (index > cursor) push({ kind: "text", text: text.slice(cursor, index) });
    const piece = match[0];
    if (piece.startsWith("`")) {
      push({ kind: "code", text: piece.slice(1, -1) });
    } else if (piece.startsWith("**")) {
      push({ kind: "strong", text: piece.slice(2, -2) });
    } else if (piece.startsWith("[")) {
      const split = piece.indexOf("](");
      const href = safeHref(piece.slice(split + 2, -1));
      if (href === null) push({ kind: "text", text: piece });
      else push({ kind: "link", text: piece.slice(1, split), href });
    } else {
      push({ kind: "em", text: piece.slice(1, -1) });
    }
    cursor = index + piece.length;
  }
  if (cursor < text.length) push({ kind: "text", text: text.slice(cursor) });
  return tokens;
}

/* -------------------------------------------------------------------------
 * Blocks
 * ---------------------------------------------------------------------- */

const HEADING = /^(#{1,4})\s+(.*)$/;
const BULLET = /^\s*[-*]\s+(.*)$/;
const NUMBERED = /^\s*\d{1,9}[.)]\s+(.*)$/;

export function parseMarkdown(source: string): Block[] {
  const blocks: Block[] = [];
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  let paragraph: string[] = [];
  let list: string[] = [];
  let ordered = false;
  let fence: string[] | null = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      // Joined with newlines, not spaces: prose rendered with `white-space:
      // normal` (the docs) collapses them exactly as a space would, while chat
      // keeps `pre-wrap` and so keeps the line breaks the writer typed.
      blocks.push({ kind: "paragraph", inline: parseInline(paragraph.join("\n").trim()) });
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list.length) {
      blocks.push({ kind: "list", ordered, items: list.map((item) => parseInline(item)) });
      list = [];
    }
  };

  for (const line of lines) {
    if (line.trimStart().startsWith("```")) {
      if (fence === null) {
        flushParagraph();
        flushList();
        fence = [];
      } else {
        blocks.push({ kind: "code", text: fence.join("\n") });
        fence = null;
      }
      continue;
    }
    if (fence !== null) {
      fence.push(line);
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({ kind: "heading", level: heading[1].length, text: heading[2].trim() });
      continue;
    }
    const bullet = BULLET.exec(line);
    const numbered = bullet ? null : NUMBERED.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const wantsOrdered = numbered !== null;
      // A change of marker starts a new list rather than mixing the two.
      if (list.length && ordered !== wantsOrdered) flushList();
      ordered = wantsOrdered;
      list.push((bullet ?? numbered)![1]);
      continue;
    }
    if (line.trim() === "") {
      flushParagraph();
      flushList();
      continue;
    }
    flushList();
    paragraph.push(line.trim());
  }
  // An unterminated fence is still a code block: model output gets truncated,
  // and half a code block reads better than its contents as prose.
  if (fence !== null) blocks.push({ kind: "code", text: fence.join("\n") });
  flushParagraph();
  flushList();
  return blocks;
}
