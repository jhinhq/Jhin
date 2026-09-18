"use client";
/* eslint-disable @next/next/no-img-element -- Model images use explicit safe URLs and must not go through a server image fetcher. */

import { lazy, Suspense } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { isExternalHref, safeHref } from "@/lib/markdown";
import "katex/dist/katex.min.css";

const RichCode = lazy(() => import("./rich-code").then((module) => ({ default: module.RichCode })));
const Diagram = lazy(() => import("./rich-code").then((module) => ({ default: module.Diagram })));

export function RichMarkdown({ source, className = "" }: { source: string; className?: string }) {
  return <div className={`rich-markdown min-w-0 space-y-3 break-words ${className}`}><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[[rehypeKatex, { trust: false, strict: "ignore", throwOnError: false }]]} urlTransform={(url) => url} components={{
    a: ({ href, children }) => href && safeHref(href) ? <a href={safeHref(href)!} rel="noreferrer noopener" target={isExternalHref(href) ? "_blank" : undefined} className="text-accent-strong underline underline-offset-2">{children}</a> : <span>{children}{href ? ` (${href})` : ""}</span>,
    img: ({ src, alt }) => typeof src === "string" && safeHref(src) ? <a href={safeHref(src)!} target="_blank" rel="noreferrer noopener"><img src={safeHref(src)!} alt={alt ?? "Agent image"} loading="lazy" referrerPolicy="no-referrer" className="max-h-96 max-w-full rounded-lg object-contain" /></a> : <span>{alt}</span>,
    pre: ({ children }) => <>{children}</>,
    code: ({ children, className: codeClass, node }) => {
      const value = String(children).replace(/\n$/, "");
      const language = /language-([^\s]+)/.exec(codeClass ?? "")?.[1];
      const block = !!language || String(children).endsWith("\n") || (node?.position?.end.line ?? 0) > (node?.position?.start.line ?? 0);
      if (!block) return <code className="rounded border border-line bg-ink/8 px-1 font-mono text-[.85em]">{children}</code>;
      return <Suspense fallback={<pre className="overflow-x-auto rounded-xl border border-line p-3 text-xs"><code>{value}</code></pre>}>{language === "mermaid" ? <Diagram source={value} /> : <RichCode source={value} language={language} />}</Suspense>;
    },
    table: ({ children }) => <div className="max-w-full overflow-x-auto rounded-lg border border-line"><table className="w-full border-collapse text-sm">{children}</table></div>,
    th: ({ children }) => <th className="border-b border-line bg-hover px-3 py-2 text-left font-semibold">{children}</th>,
    td: ({ children }) => <td className="border-b border-line px-3 py-2 align-top">{children}</td>,
    ul: ({ children }) => <ul className="list-disc space-y-1 pl-5">{children}</ul>,
    ol: ({ children, start }) => <ol start={start} className="list-decimal space-y-1 pl-5">{children}</ol>,
    blockquote: ({ children }) => <blockquote className="border-l-2 border-accent pl-3 text-dim">{children}</blockquote>,
    h1: ({ children }) => <h1 className="text-xl font-semibold">{children}</h1>,
    h2: ({ children }) => <h2 className="text-lg font-semibold">{children}</h2>,
    h3: ({ children }) => <h3 className="font-semibold">{children}</h3>,
    p: ({ children }) => <p className="whitespace-pre-wrap leading-relaxed">{children}</p>,
  }}>{source}</ReactMarkdown></div>;
}
