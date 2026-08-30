"use client";

/** Small shared pieces for the friendly company screens: status text with a
 * color dot, section cards, chips, and a simple tab strip. */

import Link from "next/link";
import { useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import type { StatusText, StatusTone } from "@/components/company/agent-helpers";
import { focusRing, rovingIndex } from "@/components/ui";

const DOT_TONES: Record<StatusTone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  neutral: "bg-faint",
  accent: "bg-accent",
  danger: "bg-danger",
};

const TEXT_TONES: Record<StatusTone, string> = {
  ok: "text-ok",
  warn: "text-warn",
  neutral: "text-dim",
  accent: "text-accent-strong",
  danger: "text-danger",
};

/** Status always pairs color with text (brief copy rules). */
export function StatusPill({ status, className = "" }: { status: StatusText; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1.5 text-[13px] font-medium ${TEXT_TONES[status.tone]} ${className}`}>
      <span aria-hidden className={`inline-block h-2 w-2 rounded-full ${DOT_TONES[status.tone]}`} />
      {status.label}
    </span>
  );
}

export function Chip({ children, href }: { children: React.ReactNode; href?: string }) {
  const className =
    "inline-flex max-w-full items-center truncate rounded-full border border-line bg-raised px-2.5 py-0.5 text-xs text-dim";
  if (href) {
    return (
      <Link href={href} className={`${className} hover:border-line-strong hover:text-ink`}>
        {children}
      </Link>
    );
  }
  return <span className={className}>{children}</span>;
}

export function SectionCard({
  title,
  description,
  action,
  children,
  className = "",
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-2xl border border-line bg-surface p-5 ${className}`}>
      <header className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h2 className="font-display text-base font-semibold tracking-tight">{title}</h2>
          {description ? <p className="mt-0.5 text-[13px] text-dim">{description}</p> : null}
        </div>
        {action ? <div className="shrink-0">{action}</div> : null}
      </header>
      {children}
    </section>
  );
}

export function StatTile({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="rounded-2xl border border-line bg-surface px-5 py-4">
      <p className="text-[13px] text-dim">{label}</p>
      <p className="mt-1 font-display text-2xl font-semibold tabular-nums tracking-tight">{value}</p>
      {hint ? <p className="mt-0.5 text-xs text-faint">{hint}</p> : null}
    </div>
  );
}

/** Accessible disclosure used for "Show details" and "Advanced". */
export function Disclosure({
  label,
  openLabel,
  children,
  defaultOpen = false,
}: {
  label: string;
  openLabel?: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((value) => !value)}
        className="text-xs font-medium text-accent-strong hover:underline"
      >
        {open ? (openLabel ?? `Hide ${label.replace(/^Show /, "")}`) : label}
      </button>
      {open ? (
        <div id={id} className="mt-2">
          {children}
        </div>
      ) : null}
    </div>
  );
}

/** Underline-style tab strip with the same roving-tabindex keyboard behavior
 * as the pill Tabs in components/ui.tsx: arrows, Home, and End move selection,
 * and only the active tab sits in the tab order. */
export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: { id: T; label: string; count?: number }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  const refs = useRef<Map<T, HTMLButtonElement>>(new Map());

  const focusTab = (id: T) => {
    refs.current.get(id)?.focus();
    onChange(id);
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, id: T) => {
    const index = tabs.findIndex((tab) => tab.id === id);
    if (index === -1 || tabs.length === 0) return;
    const next = rovingIndex(event.key, index, tabs.length);
    if (next === null) return;
    event.preventDefault();
    focusTab(tabs[next].id);
  };

  return (
    <div role="tablist" aria-label={label} className="flex gap-1 overflow-x-auto border-b border-line">
      {tabs.map((tab) => {
        const active = tab.id === value;
        return (
          <button
            key={tab.id}
            ref={(node) => {
              if (node) refs.current.set(tab.id, node);
              else refs.current.delete(tab.id);
            }}
            type="button"
            role="tab"
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(tab.id)}
            onKeyDown={(event) => onKeyDown(event, tab.id)}
            className={`flex h-10 shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 text-sm transition-colors ${focusRing} ${
              active ? "border-accent font-medium text-ink" : "border-transparent text-dim hover:text-ink"
            }`}
          >
            {tab.label}
            {tab.count !== undefined ? (
              <span className="rounded-full bg-hover px-1.5 text-[11px] tabular-nums text-dim">{tab.count}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

/** Segmented control with two or more options, used for view toggles and
 * filters. Renders as a radio group for screen readers. */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: { id: T; label: string; icon?: React.ReactNode }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-xl border border-line bg-raised p-0.5">
      {options.map((option) => {
        const active = option.id === value;
        return (
          <button
            key={option.id}
            role="radio"
            aria-checked={active}
            onClick={() => onChange(option.id)}
            className={`inline-flex h-9 items-center gap-1.5 rounded-[10px] px-3 text-[13px] transition-colors ${
              active ? "bg-surface font-medium text-ink shadow-sm" : "text-dim hover:text-ink"
            }`}
          >
            {option.icon}
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/** Every error says what failed and a next step. */
export function LoadError({ what, onRetry }: { what: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="rounded-2xl border border-danger/30 bg-danger-soft px-4 py-3 text-sm text-danger">
      <p className="font-medium">We couldn’t load {what}.</p>
      <p className="mt-0.5 text-[13px]">
        Check your connection and try again.{" "}
        {onRetry ? (
          <button type="button" onClick={onRetry} className="font-medium underline">
            Retry
          </button>
        ) : null}
      </p>
    </div>
  );
}
