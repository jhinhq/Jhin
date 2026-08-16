"use client";

/** Small hand-rolled UI kit: distinct visual system, no component library. */

import { X } from "lucide-react";
import { useEffect } from "react";

type ButtonVariant = "primary" | "ghost" | "outline" | "danger";

const buttonStyles: Record<ButtonVariant, string> = {
  primary:
    "bg-accent text-[#0b0c14] font-semibold hover:bg-accent-strong disabled:opacity-40",
  ghost: "text-dim hover:text-ink hover:bg-hover",
  outline: "border border-line-strong text-ink hover:bg-hover hover:border-faint",
  danger: "bg-danger/10 text-danger border border-danger/30 hover:bg-danger/20",
};

export function Button({
  variant = "outline",
  size = "md",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: "sm" | "md";
}) {
  const sizing = size === "sm" ? "h-7 px-2.5 text-xs" : "h-9 px-3.5 text-sm";
  return (
    <button
      className={`inline-flex items-center justify-center gap-1.5 rounded-md transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${sizing} ${buttonStyles[variant]} ${className}`}
      {...props}
    />
  );
}

const fieldStyles =
  "w-full rounded-md border border-line bg-raised px-3 text-sm text-ink placeholder:text-faint outline-none focus:border-accent focus:ring-2 focus:ring-accent/25 disabled:opacity-50";

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  const { className = "", ...rest } = props;
  return <input className={`h-9 ${fieldStyles} ${className}`} {...rest} />;
}

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const { className = "", ...rest } = props;
  return <textarea className={`py-2 ${fieldStyles} ${className}`} {...rest} />;
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  const { className = "", ...rest } = props;
  return <select className={`h-9 appearance-none ${fieldStyles} ${className}`} {...rest} />;
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
      <span className="text-xs font-medium uppercase tracking-wider text-dim">{label}</span>
      {children}
      {hint ? <span className="block text-xs text-faint">{hint}</span> : null}
    </label>
  );
}

export function ErrorNote({ message }: { message: string | null | undefined }) {
  if (!message) return null;
  return (
    <p
      role="alert"
      className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger"
    >
      {message}
    </p>
  );
}

export function Badge({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "ok" | "warn" | "danger" | "accent";
  children: React.ReactNode;
}) {
  const tones = {
    neutral: "bg-hover text-dim border-line-strong",
    ok: "bg-ok/10 text-ok border-ok/30",
    warn: "bg-warn/10 text-warn border-warn/30",
    danger: "bg-danger/10 text-danger border-danger/30",
    accent: "bg-accent-soft text-accent-strong border-accent/30",
  } as const;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

export function StatusDot({ status }: { status: "active" | "paused" | "disabled" }) {
  const color =
    status === "active" ? "bg-ok" : status === "paused" ? "bg-warn" : "bg-faint";
  return <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${color}`} />;
}

export function Dialog({
  title,
  open,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 pt-[10vh]">
      <button
        aria-label="Close dialog"
        className="fixed inset-0 cursor-default"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-label={title}
        className={`relative w-full ${wide ? "max-w-2xl" : "max-w-md"} rounded-xl border border-line bg-surface shadow-2xl shadow-black/50`}
      >
        <header className="flex items-center justify-between border-b border-line px-5 py-3.5">
          <h2 className="text-sm font-semibold">{title}</h2>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close">
            <X size={14} />
          </Button>
        </header>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-dim">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-line-strong border-t-accent" />
      {label ?? "Loading…"}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-line-strong px-8 py-14 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      <p className="max-w-sm text-sm text-dim">{description}</p>
      {action}
    </div>
  );
}
