"use client";

/** Autosizing chat composer. Enter sends, Shift+Enter adds a newline. */

import { SendHorizontal, Square } from "lucide-react";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from "react";
import { focusRing } from "@/components/ui";

export interface ComposerHandle {
  focus: () => void;
}

/**
 * Horizontal padding is the *whole* inset between the rounded shell and the
 * text, so it must be a symmetric `px-*` and must not depend on how many
 * trailing controls are rendered — that is what keeps the field optically
 * centred. Exported so the tests can assert the contract directly.
 */
export const COMPOSER_FIELD_PAD = {
  large: "px-5 pb-2 pt-4 text-base",
  docked: "px-4 pb-1.5 pt-3 text-[15px]",
} as const;

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
    /** Show a Stop control: there's an active task (running/queued/paused)
     * on this conversation that can be cancelled. */
    canStop?: boolean;
    /** Opens the stop confirmation. Reuses the same cancel mutation and
     * confirm dialog as the header/Details controls. */
    onStop?: () => void;
    /** True while a pause/resume/cancel request is in flight — disables the
     * Stop button so it can't be double-fired. */
    stopping?: boolean;
    /** Accessible name for the Stop button, e.g. "Stop Scout". */
    stopLabel?: string;
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
    canStop = false,
    onStop,
    stopping = false,
    stopLabel = "Stop",
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

  // The text field owns a full-width row of its own, so its horizontal
  // padding is the whole inset on both sides: left inset === right inset,
  // whatever trailing controls happen to be visible. The controls sit on
  // their own row underneath instead of eating into the field's width.
  const fieldPad = variant === "large" ? COMPOSER_FIELD_PAD.large : COMPOSER_FIELD_PAD.docked;
  const controlsPad = variant === "large" ? "px-3 pb-3" : "px-2 pb-2";

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
      className="space-y-2"
    >
      <div
        data-testid="composer-shell"
        className={`flex flex-col rounded-2xl border bg-surface shadow-[var(--card-shadow)] transition-colors focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/25 ${
          disabled ? "border-line opacity-70" : "border-line-strong"
        }`}
      >
        <label className="block min-w-0">
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
            className={`block w-full resize-none bg-transparent leading-relaxed text-ink placeholder:text-faint outline-none disabled:cursor-not-allowed ${fieldPad}`}
          />
        </label>
        <div
          data-testid="composer-controls"
          className={`flex shrink-0 items-center justify-end gap-2 ${controlsPad}`}
        >
          {canStop ? (
            <button
              type="button"
              data-testid="composer-stop"
              aria-label={stopLabel}
              title={stopLabel}
              disabled={stopping}
              onClick={onStop}
              className="inline-flex h-10 shrink-0 items-center gap-1.5 rounded-xl border border-line-strong px-3 text-xs font-medium text-dim transition-colors hover:border-danger/40 hover:bg-danger/10 hover:text-danger disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Square size={13} aria-hidden fill="currentColor" />
              Stop
            </button>
          ) : null}
          <button
            type="submit"
            aria-label={sending ? "Sending…" : "Send message"}
            disabled={!canSend}
            className={`btn-gradient inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl disabled:cursor-not-allowed disabled:opacity-40 ${focusRing}`}
          >
            {sending ? (
              <span
                aria-hidden
                className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
              />
            ) : (
              <SendHorizontal size={18} />
            )}
          </button>
        </div>
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
