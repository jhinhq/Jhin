"use client";

/** The one markdown renderer. Both callers — the API reference (prose from our
 * own OpenAPI document) and the chat transcript (prose from a model) — render
 * through here, so the link hardening in `lib/markdown` protects both.
 *
 * Everything below builds React elements. There is no `dangerouslySetInnerHTML`
 * anywhere in this file, and there must never be one: that is what makes raw
 * HTML in the source show up as text instead of running. */

import { focusRing } from "@/components/ui";
import { isExternalHref, parseMarkdown } from "@/lib/markdown";
import type { Block, Inline } from "@/lib/markdown";

export type MarkdownVariant = "docs" | "chat";

type Skin = {
  root: string;
  paragraph: string;
  heading: (level: number) => string;
  list: string;
  pre: string;
  strong: string;
  code: string;
  link: string;
};

const SKINS: Record<MarkdownVariant, Skin> = {
  /** Reference prose: quiet body text, emphasis in full-strength ink. */
  docs: {
    root: "space-y-3 text-sm leading-relaxed text-dim",
    paragraph: "",
    heading: () => "pt-2 font-display text-sm font-semibold text-ink",
    list: "space-y-1 pl-5",
    pre: "overflow-x-auto rounded-xl bg-hover p-3 font-mono text-[12px] leading-relaxed text-ink",
    strong: "font-semibold text-ink",
    code: "rounded bg-hover px-1 py-0.5 font-mono text-[0.85em] text-ink",
    link: `text-accent-strong underline underline-offset-2 ${focusRing}`,
  },
  /** Inside a chat bubble: inherit the bubble's size, leading and colour so the
   * same markup reads correctly on the surface bubble and on the accent-tinted
   * one. Code tints with the text colour rather than a surface token, which is
   * the only way to stay visible on both backgrounds and in both themes. */
  chat: {
    root: "space-y-2 break-words",
    paragraph: "whitespace-pre-wrap",
    heading: (level) =>
      `font-display font-semibold ${level <= 2 ? "text-[16px]" : "text-[15px]"}`,
    list: "space-y-1 pl-5",
    pre: "overflow-x-auto rounded-xl border border-line bg-ink/8 p-3 font-mono text-[12.5px] leading-relaxed",
    strong: "font-semibold",
    code: "rounded border border-line bg-ink/8 px-1 py-0.5 font-mono text-[0.85em]",
    link: `font-medium text-accent-strong underline underline-offset-2 ${focusRing}`,
  },
};

export function InlineRun({ tokens, skin }: { tokens: Inline[]; skin: Skin }) {
  return (
    <>
      {tokens.map((token, index) => {
        switch (token.kind) {
          case "code":
            return (
              <code key={index} className={skin.code}>
                {token.text}
              </code>
            );
          case "strong":
            return (
              <strong key={index} className={skin.strong}>
                {token.text}
              </strong>
            );
          case "em":
            return <em key={index}>{token.text}</em>;
          case "link":
            // `href` already survived `safeHref`; an unsafe one never reaches
            // this branch, it arrives as a text token.
            return (
              <a
                key={index}
                href={token.href}
                className={skin.link}
                rel="noreferrer noopener"
                target={isExternalHref(token.href) ? "_blank" : undefined}
              >
                {token.text}
              </a>
            );
          default:
            return <span key={index}>{token.text}</span>;
        }
      })}
    </>
  );
}

export function Markdown({
  source,
  className = "",
  variant = "docs",
}: {
  source: string;
  className?: string;
  variant?: MarkdownVariant;
}) {
  const skin = SKINS[variant];
  const blocks: Block[] = parseMarkdown(source);
  return (
    <div className={`${skin.root} ${className}`}>
      {blocks.map((block, index) => {
        if (block.kind === "heading") {
          return (
            <h3 key={index} className={skin.heading(block.level)}>
              {block.text}
            </h3>
          );
        }
        if (block.kind === "code") {
          // `min-w-0` on the parent plus `overflow-x-auto` here: a long line
          // scrolls inside the block instead of widening whatever contains it.
          return (
            <pre key={index} className={skin.pre}>
              <code>{block.text}</code>
            </pre>
          );
        }
        if (block.kind === "list") {
          const List = block.ordered ? "ol" : "ul";
          return (
            <List
              key={index}
              className={`${block.ordered ? "list-decimal" : "list-disc"} ${skin.list}`}
            >
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>
                  <InlineRun tokens={item} skin={skin} />
                </li>
              ))}
            </List>
          );
        }
        return (
          <p key={index} className={skin.paragraph}>
            <InlineRun tokens={block.inline} skin={skin} />
          </p>
        );
      })}
    </div>
  );
}
