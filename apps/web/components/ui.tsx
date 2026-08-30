"use client";

/** Small hand-rolled UI kit for Jhin (2026-08-21 redesign): no component
 * library. Tokens live in app/globals.css; every primitive here is theme-aware
 * and keyboard accessible. */

import { Moon, Sun, X } from "lucide-react";
import Link from "next/link";
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useSyncExternalStore,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

/* ------------------------------------------------------------------ */
/* Buttons                                                             */
/* ------------------------------------------------------------------ */

type ButtonVariant = "primary" | "ghost" | "outline" | "danger";

const buttonStyles: Record<ButtonVariant, string> = {
  primary: "btn-gradient font-semibold",
  ghost: "text-dim hover:text-ink hover:bg-hover",
  outline:
    "border border-line-strong bg-surface text-ink hover:border-accent hover:bg-hover",
  danger: "bg-danger-soft text-danger border border-danger/30 hover:border-danger",
};

export const focusRing =
  "outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg";

export function Button({
  variant = "outline",
  size = "md",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: "sm" | "md";
}) {
  // "sm" is a desktop density; below md (phones) it grows to the 40px touch
  // floor the design brief requires.
  const sizing = size === "sm" ? "h-10 px-3 text-[13px] md:h-8" : "h-10 px-4 text-sm";
  return (
    <button
      className={`inline-flex shrink-0 items-center justify-center gap-1.5 rounded-xl font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${focusRing} ${sizing} ${buttonStyles[variant]} ${className}`}
      {...props}
    />
  );
}

/** A navigation link dressed exactly like Button. Use this instead of
 * wrapping a Button in a Link: a button inside an anchor is invalid HTML and
 * hands keyboard and screen-reader users two nested stops for one control. */
export function ButtonLink({
  variant = "outline",
  size = "md",
  className = "",
  ...props
}: React.ComponentProps<typeof Link> & {
  variant?: ButtonVariant;
  size?: "sm" | "md";
}) {
  const sizing = size === "sm" ? "h-10 px-3 text-[13px] md:h-8" : "h-10 px-4 text-sm";
  return (
    <Link
      className={`inline-flex shrink-0 items-center justify-center gap-1.5 rounded-xl font-medium transition-colors ${focusRing} ${sizing} ${buttonStyles[variant]} ${className}`}
      {...props}
    />
  );
}

/** Square icon-only button. Requires an `aria-label`; the label doubles as a
 * native tooltip via `title`. */
function IconButton({
  label,
  size = "md",
  variant = "ghost",
  className = "",
  children,
  ...props
}: Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "aria-label" | "children"> & {
  label: string;
  size?: "sm" | "md";
  variant?: ButtonVariant;
  children: React.ReactNode;
}) {
  const sizing = size === "sm" ? "h-10 w-10 md:h-8 md:w-8" : "h-10 w-10";
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={`inline-flex shrink-0 items-center justify-center rounded-xl transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${focusRing} ${sizing} ${buttonStyles[variant]} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------ */
/* Form fields                                                         */
/* ------------------------------------------------------------------ */

// text-base below md: iOS Safari zooms the whole page (and leaves it
// scrolled sideways) when a focused control's font-size is under 16px.
const fieldStyles =
  "w-full rounded-xl border border-line bg-surface px-3.5 text-base md:text-[15px] text-ink placeholder:text-faint outline-none transition-colors hover:border-line-strong focus:border-accent focus:ring-2 focus:ring-accent/25 disabled:opacity-50";

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  const { className = "", ...rest } = props;
  return <input className={`h-10 ${fieldStyles} ${className}`} {...rest} />;
}

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const { className = "", ...rest } = props;
  return <textarea className={`py-2.5 ${fieldStyles} ${className}`} {...rest} />;
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  const { className = "", ...rest } = props;
  return (
    <span className="relative block">
      <select className={`h-10 appearance-none pr-9 ${fieldStyles} ${className}`} {...rest} />
      <svg
        aria-hidden
        viewBox="0 0 20 20"
        className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M6 8l4 4 4-4" />
      </svg>
    </span>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block space-y-1.5">
      <span className="text-[13px] font-medium text-dim">{label}</span>
      {children}
      {hint ? <span className="block text-[13px] text-faint">{hint}</span> : null}
    </label>
  );
}

export function ErrorNote({ message }: { message: string | null | undefined }) {
  if (!message) return null;
  return (
    <p
      role="alert"
      className="rounded-xl border border-danger/30 bg-danger-soft px-3.5 py-2.5 text-sm text-danger"
    >
      {message}
    </p>
  );
}

/* ------------------------------------------------------------------ */
/* Status                                                              */
/* ------------------------------------------------------------------ */

export type BadgeTone = "neutral" | "ok" | "warn" | "danger" | "accent" | "info";

const badgeTones: Record<BadgeTone, string> = {
  neutral: "bg-raised text-dim border-line",
  ok: "bg-ok-soft text-ok border-ok/30",
  warn: "bg-warn-soft text-warn border-warn/30",
  danger: "bg-danger-soft text-danger border-danger/30",
  accent: "bg-accent-soft text-accent-strong border-accent/30",
  info: "bg-info-soft text-info border-info/30",
};

export function Badge({
  tone = "neutral",
  children,
}: {
  tone?: BadgeTone;
  children: React.ReactNode;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium ${badgeTones[tone]}`}
    >
      {children}
    </span>
  );
}

export function StatusDot({ status }: { status: "active" | "paused" | "disabled" }) {
  const color =
    status === "active" ? "bg-ok" : status === "paused" ? "bg-warn" : "bg-faint";
  return <span aria-hidden className={`inline-block h-2 w-2 shrink-0 rounded-full ${color}`} />;
}

const dotTones: Record<BadgeTone, string> = {
  neutral: "bg-faint",
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-danger",
  accent: "bg-accent",
  info: "bg-info",
};

/** Colored dot paired with text so status is never color-only. */
export function StatusLabel({
  tone = "neutral",
  children,
  className = "",
}: {
  tone?: BadgeTone;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span className={`inline-flex items-center gap-2 text-sm text-ink ${className}`}>
      <span className="relative inline-flex h-2 w-2 shrink-0">
        <span aria-hidden className={`relative inline-block h-2 w-2 rounded-full ${dotTones[tone]}`} />
      </span>
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Layout                                                              */
/* ------------------------------------------------------------------ */

export function Card({
  as: Tag = "div",
  className = "",
  children,
  ...props
}: React.HTMLAttributes<HTMLElement> & {
  as?: "div" | "section" | "article" | "li";
  children: React.ReactNode;
}) {
  return (
    <Tag
      className={`rounded-2xl border border-line bg-surface shadow-card p-5 ${className}`}
      {...props}
    >
      {children}
    </Tag>
  );
}

/* ------------------------------------------------------------------ */
/* Tabs                                                                */
/* ------------------------------------------------------------------ */

export interface TabItem {
  id: string;
  label: React.ReactNode;
  disabled?: boolean;
}

/** Shared roving-tabindex arithmetic: the index a key press moves to, or null
 * when the key is not part of the pattern. Used by both Tabs implementations
 * and the model-picker radio group so the keyboard contract lives once. */
export function rovingIndex(key: string, index: number, length: number): number | null {
  if (key === "ArrowRight" || key === "ArrowDown") return (index + 1) % length;
  if (key === "ArrowLeft" || key === "ArrowUp") return (index - 1 + length) % length;
  if (key === "Home") return 0;
  if (key === "End") return length - 1;
  return null;
}

/** Roving-tabindex tab list. Selection follows focus (arrow keys, Home, End);
 * `panelId` wires aria-controls when the page renders a matching panel. */
export function Tabs({
  tabs,
  value,
  onChange,
  label,
  panelId,
  className = "",
}: {
  tabs: TabItem[];
  value: string;
  onChange: (id: string) => void;
  label: string;
  panelId?: string;
  className?: string;
}) {
  const refs = useRef<Map<string, HTMLButtonElement>>(new Map());
  const enabled = tabs.filter((tab) => !tab.disabled);

  const focusTab = (id: string) => {
    refs.current.get(id)?.focus();
    onChange(id);
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, id: string) => {
    const index = enabled.findIndex((tab) => tab.id === id);
    if (index === -1 || enabled.length === 0) return;
    const next = rovingIndex(event.key, index, enabled.length);
    if (next === null) return;
    event.preventDefault();
    focusTab(enabled[next].id);
  };

  return (
    <div
      role="tablist"
      aria-label={label}
      className={`inline-flex max-w-full gap-1 overflow-x-auto rounded-xl border border-line bg-raised p-1 ${className}`}
    >
      {tabs.map((tab) => {
        const selected = tab.id === value;
        return (
          <button
            key={tab.id}
            ref={(node) => {
              if (node) refs.current.set(tab.id, node);
              else refs.current.delete(tab.id);
            }}
            type="button"
            role="tab"
            id={`tab-${tab.id}`}
            aria-selected={selected}
            aria-controls={panelId}
            aria-disabled={tab.disabled || undefined}
            disabled={tab.disabled}
            tabIndex={selected ? 0 : -1}
            onClick={() => !tab.disabled && onChange(tab.id)}
            onKeyDown={(event) => onKeyDown(event, tab.id)}
            className={`inline-flex h-10 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg px-3 text-[13px] font-medium transition-colors disabled:opacity-40 md:h-8 ${focusRing} ${
              selected
                ? "bg-surface text-ink shadow-card"
                : "text-dim hover:bg-hover hover:text-ink"
            }`}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Dialog                                                              */
/* ------------------------------------------------------------------ */

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Dialog({
  title,
  open,
  onClose,
  children,
  wide = false,
  description,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
  description?: string;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    // Move focus inside: first control in the body, else Close, else the panel.
    const first =
      bodyRef.current?.querySelector<HTMLElement>(FOCUSABLE) ??
      panel?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? panel)?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !panel) return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => !el.closest("[hidden]") && el.getAttribute("aria-hidden") !== "true",
      );
      if (focusable.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const firstEl = focusable[0];
      const lastEl = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === firstEl || active === panel)) {
        event.preventDefault();
        lastEl.focus();
      } else if (!event.shiftKey && active === lastEl) {
        event.preventDefault();
        firstEl.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      previouslyFocused?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-[#221e38]/40 p-4 pt-[10vh] backdrop-blur-sm dark:bg-black/60">
      <button
        type="button"
        aria-label="Close dialog"
        tabIndex={-1}
        className="fixed inset-0 cursor-default"
        onClick={onClose}
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        className={`relative w-full outline-none ${wide ? "max-w-2xl" : "max-w-md"} rounded-2xl border border-line bg-surface shadow-card`}
      >
        <header className="flex items-start justify-between gap-4 border-b border-line px-6 py-4">
          <div className="min-w-0">
            <h2 id={titleId} className="font-display text-base font-semibold tracking-tight">
              {title}
            </h2>
            {description ? (
              <p id={descId} className="mt-0.5 text-sm text-dim">
                {description}
              </p>
            ) : null}
          </div>
          <IconButton label="Close" size="sm" onClick={onClose} className="-mr-2 -mt-1">
            <X size={16} />
          </IconButton>
        </header>
        <div ref={bodyRef} className="p-6">
          {children}
        </div>
      </div>
    </div>
  );
}

/** The shared yes-or-no dialog that replaces `window.confirm`: a title, a body
 * that can carry real consequences, and two buttons. `busy` freezes both while
 * the confirmed action is in flight so a double-click cannot fire it twice. */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  cancelLabel = "Cancel",
  tone = "danger",
  busy = false,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  body: React.ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: "danger" | "primary";
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Dialog title={title} open={open} onClose={onClose}>
      <div className="space-y-5" data-testid="confirm-dialog">
        <div className="text-sm text-dim">{body}</div>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>
            {cancelLabel}
          </Button>
          <Button
            type="button"
            variant={tone === "danger" ? "danger" : "primary"}
            disabled={busy}
            onClick={onConfirm}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ */
/* Feedback                                                            */
/* ------------------------------------------------------------------ */

export function Spinner({ label }: { label?: string }) {
  return (
    <div role="status" className="flex items-center gap-2 text-sm text-dim">
      <span
        aria-hidden
        className="h-4 w-4 animate-spin rounded-full border-2 border-line-strong border-t-accent"
      />
      {label ?? "Loading…"}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
  icon,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
  icon?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-line-strong bg-surface/60 px-8 py-14 text-center">
      {icon ? (
        <div className="mb-1 flex h-12 w-12 items-center justify-center rounded-2xl bg-accent-soft text-accent-strong">
          {icon}
        </div>
      ) : null}
      <p className="font-display text-base font-semibold text-ink">{title}</p>
      <p className="max-w-sm text-sm text-dim">{description}</p>
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Theme                                                               */
/* ------------------------------------------------------------------ */

const THEME_STORAGE_KEY = "jhin-theme";

function subscribeTheme(onChange: () => void) {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  return () => observer.disconnect();
}
const readTheme = () => document.documentElement.classList.contains("dark");
const readServerTheme = () => false;

/** True when `<html>` carries the `.dark` class (set by the no-flash script in
 * app/layout.tsx). Safe to call during SSR (returns false). */
function useIsDark(): boolean {
  return useSyncExternalStore(subscribeTheme, readTheme, readServerTheme);
}

/** Sun/moon toggle. Reads the `.dark` class and persists the choice to
 * localStorage["jhin-theme"]. */
export function ThemeToggle({
  showLabel = false,
}: {
  showLabel?: boolean;
}) {
  const dark = useIsDark();

  const toggle = useCallback(() => {
    const next = !document.documentElement.classList.contains("dark");
    document.documentElement.classList.toggle("dark", next);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next ? "dark" : "light");
    } catch {
      /* storage unavailable (private mode) */
    }
  }, []);

  const label = dark ? "Switch to light theme" : "Switch to dark theme";
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={label}
      aria-pressed={dark}
      title={label}
      data-theme={dark ? "dark" : "light"}
      className={`inline-flex h-10 items-center justify-center gap-2 rounded-xl border border-line text-ink transition-colors hover:border-accent hover:bg-hover ${showLabel ? "px-3" : "w-10"} ${focusRing}`}
    >
      {dark ? <Moon size={16} strokeWidth={1.8} /> : <Sun size={16} strokeWidth={1.8} />}
      {showLabel ? (
        <span className="text-[13px] font-medium">{dark ? "Dark" : "Light"}</span>
      ) : null}
    </button>
  );
}
