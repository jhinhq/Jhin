"use client";

/** Autosizing chat composer. Enter sends, Shift+Enter adds a newline. */

import { SendHorizontal } from "lucide-react";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from "react";

export interface ComposerHandle {
  focus: () => void;
}

export const Composer = forwardRef<
  ComposerHandle,
  {
    value: string;
    onChange: (value: string) => void;
    onSend: (text: string) => void;
    sending?: boolean;
    disabled?: boolean;
    /** Plain-language reason shown when the composer is disabled. */
    disabledReason?: string | null;
    placeholder?: string;
    autoFocus?: boolean;
    /** "large" is the home hero; "docked" sits at the bottom of a thread. */
    variant?: "large" | "docked";
    hint?: string | null;
  }
>(function Composer(
  {
    value,
    onChange,
    onSend,
    sending = false,
    disabled = false,
    disabledReason = null,
    placeholder = "Message…",
    autoFocus = false,
    variant = "docked",
    hint = null,
  },
  ref,
) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => ({ focus: () => textareaRef.current?.focus() }), []);

  const maxHeight = variant === "large" ? 280 : 200;
  const resize = useCallback(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = "auto";
    if (node.value.trim() === "") {
      // Empty: let rows={1} define the height (placeholder wrapping in a
      // collapsed layout must not pin a tall box).
      node.style.overflowY = "hidden";
      return;
    }
    node.style.height = `${Math.min(node.scrollHeight, maxHeight)}px`;
    node.style.overflowY = node.scrollHeight > maxHeight ? "auto" : "hidden";
  }, [maxHeight]);

  useEffect(() => {
    resize();
  }, [value, resize]);

  // Re-measure when the available width changes (window resize, rotation,
  // panel open/close): wrapped lines change the needed height.
  useEffect(() => {
    const node = textareaRef.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => resize());
    observer.observe(node.parentElement ?? node);
    return () => observer.disconnect();
  }, [resize]);

  const canSend = !disabled && !sending && value.trim().length > 0;

  const submit = () => {
    if (!canSend) return;
    onSend(value.trim());
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  const pad = variant === "large" ? "px-5 py-4 text-base" : "px-4 py-3 text-[15px]";

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
      className="space-y-2"
    >
      <div
        className={`flex items-end gap-2 rounded-2xl border bg-surface shadow-[var(--card-shadow)] transition-colors focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/25 ${
          disabled ? "border-line opacity-70" : "border-line-strong"
        }`}
      >
        <label className="min-w-0 flex-1">
          <span className="sr-only">Message</span>
          <textarea
            ref={textareaRef}
            rows={1}
            value={value}
            autoFocus={autoFocus}
            disabled={disabled}
            aria-disabled={disabled || undefined}
            aria-describedby={disabled && disabledReason ? "composer-disabled-reason" : undefined}
            placeholder={disabled && disabledReason ? disabledReason : placeholder}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={onKeyDown}
            className={`block w-full resize-none bg-transparent leading-relaxed text-ink placeholder:text-faint outline-none disabled:cursor-not-allowed ${pad}`}
          />
        </label>
        <button
          type="submit"
          aria-label="Send message"
          disabled={!canSend}
          className="m-2 inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent text-white shadow-[0_6px_20px_-6px_var(--glow,rgba(115,113,252,0.4))] transition-transform hover:-translate-y-px disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:translate-y-0"
        >
          <SendHorizontal size={18} />
        </button>
      </div>
      {disabled && disabledReason ? (
        <p id="composer-disabled-reason" className="px-2 text-xs text-dim">
          {disabledReason}
        </p>
      ) : hint ? (
        <p className="px-2 text-xs text-faint">{hint}</p>
      ) : (
        <p className="px-2 text-xs text-faint">
          Enter to send · Shift+Enter for a new line
        </p>
      )}
    </form>
  );
});
