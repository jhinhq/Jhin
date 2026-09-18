"use client";
/* eslint-disable @next/next/no-img-element -- Inert data-URL SVG rendering is the security boundary for diagrams. */

import { useEffect, useId, useState } from "react";
import type { ThemedToken } from "shiki";
let highlighter: Promise<import("shiki").Highlighter> | undefined;

export function RichCode({ source, language = "text" }: { source: string; language?: string }) {
  const [highlighted, setHighlighted] = useState<{ source: string; language: string; tokens: ThemedToken[][] } | null>(null);
  const tokens = highlighted?.source === source && highlighted.language === language ? highlighted.tokens : null;
  const [copied, setCopied] = useState<string | null>(null);
  const [copyFailed, setCopyFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    if (source.length > 100_000) return;
    void import("shiki").then(async ({ createHighlighter, createJavaScriptRegexEngine, bundledLanguages }) => {
      const lang = language in bundledLanguages ? language as keyof typeof bundledLanguages : "text";
      highlighter ??= createHighlighter({ themes: ["github-dark"], langs: [], engine: createJavaScriptRegexEngine() });
      const instance = await highlighter;
      if (lang !== "text") await instance.loadLanguage(lang);
      const result = instance.codeToTokens(source, { lang, theme: "github-dark" });
      if (alive) setHighlighted({ source, language, tokens: result.tokens });
    }).catch(() => {});
    return () => { alive = false; };
  }, [source, language]);
  return <div className="my-2 min-w-0 overflow-hidden rounded-xl border border-line bg-[#24292e] text-[#e1e4e8]">
    <div className="flex items-center justify-between border-b border-white/10 px-3 py-1 text-[11px]">
      <span>{language}</span><button type="button" className="min-h-8 rounded px-2 hover:bg-white/10" onClick={() => { if (!navigator.clipboard) { setCopyFailed(true); return; } void navigator.clipboard.writeText(source).then(() => { setCopied(source); setCopyFailed(false); }).catch(() => setCopyFailed(true)); }}>{copied === source ? "Copied" : copyFailed ? "Select code to copy" : "Copy code"}</button>
    </div>
    <pre className="overflow-x-auto p-3 font-mono text-xs leading-relaxed"><code>{tokens ? tokens.map((line, i) => <span key={i}>{line.map((token, j) => <span key={j} style={{ color: token.color }}>{token.content}</span>)}{i < tokens.length - 1 ? "\n" : ""}</span>) : source}</code></pre>
  </div>;
}

export function Diagram({ source }: { source: string }) {
  const id = useId().replace(/[^a-z0-9]/gi, "");
  const [rendered, setRendered] = useState<{ source: string; url: string } | null>(null);
  const image = rendered?.source === source ? rendered.url : null;
  useEffect(() => {
    let alive = true;
    if (source.length > 20_000) return;
    void import("mermaid").then(async ({ default: mermaid }) => {
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", maxTextSize: 20_000, suppressErrorRendering: true });
      const result = await mermaid.render(`diagram${id}`, source);
      // An image document cannot execute SVG scripts or navigate the host page.
      if (alive) setRendered({ source, url: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(result.svg)}` });
    }).catch(() => {});
    return () => { alive = false; };
  }, [id, source]);
  return <div className="min-w-0">{image ? <img src={image} alt="Diagram" className="max-h-[32rem] max-w-full rounded bg-white p-3" /> : null}<details open={!image}><summary className="cursor-pointer text-xs text-dim">Diagram source</summary><RichCode source={source} language="mermaid" /></details></div>;
}
