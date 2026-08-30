"use client";

/**
 * Keyboard/pointer equivalent of dragging an agent around the org chart.
 *
 * Drag-and-drop is never the only way to reorganise: every agent card an
 * admin can move also carries this "Move…" button, which opens a menu of
 * every team's top level plus every possible manager. Illegal choices stay
 * in the list, disabled, with the reason spelled out — the same reasons the
 * drag path shows — so the refusal is explained instead of hidden.
 *
 * The menu is portalled to <body> because the Map view scrolls horizontally
 * and would otherwise clip it; its position is written straight onto the DOM
 * node after layout rather than round-tripped through React state.
 */

import { Ban, Check, CornerUpRight } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { focusRing } from "@/components/ui";
import { moveOptions, type MoveGraph, type MoveTarget } from "@/lib/org-move";
import type { OrgAgentNode } from "@/lib/types";

const MENU_WIDTH = 288;
const MENU_MAX_HEIGHT = 320;

export function MoveMenu({
  agent,
  graph,
  onMove,
}: {
  agent: OrgAgentNode;
  graph: MoveGraph;
  onMove: (target: MoveTarget) => void;
}) {
  const [open, setOpen] = useState(false);
  const [trigger, setTrigger] = useState<HTMLButtonElement | null>(null);
  const menuId = useId();

  const close = useCallback(
    (refocus: boolean) => {
      setOpen(false);
      if (refocus) trigger?.focus();
    },
    [trigger],
  );

  return (
    <>
      <button
        ref={setTrigger}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((value) => !value)}
        title={`Move ${agent.name}`}
        className={`inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-transparent text-dim transition-colors hover:border-line hover:bg-hover hover:text-ink md:h-8 md:w-8 ${focusRing}`}
      >
        <CornerUpRight size={14} aria-hidden />
        <span className="sr-only">Move {agent.name}</span>
      </button>
      {open ? (
        <MovePopup
          id={menuId}
          agent={agent}
          graph={graph}
          trigger={trigger}
          onClose={close}
          onPick={(target) => {
            close(true);
            onMove(target);
          }}
        />
      ) : null}
    </>
  );
}

function MovePopup({
  id,
  agent,
  graph,
  trigger,
  onClose,
  onPick,
}: {
  id: string;
  agent: OrgAgentNode;
  graph: MoveGraph;
  trigger: HTMLButtonElement | null;
  onClose: (refocus: boolean) => void;
  onPick: (target: MoveTarget) => void;
}) {
  const groups = moveOptions(agent.id, graph);
  const menuRef = useRef<HTMLDivElement | null>(null);

  /** Place the menu beside its trigger and move focus into it. A callback ref
   * keeps this a pure DOM side effect at commit time: no React state, and so
   * no cascading render. */
  const placeMenu = useCallback(
    (node: HTMLDivElement | null) => {
      menuRef.current = node;
      if (!node) return;
      const rect = trigger?.getBoundingClientRect();
      if (rect && (rect.width > 0 || rect.height > 0)) {
        const left = Math.max(
          8,
          Math.min(rect.right - MENU_WIDTH, window.innerWidth - MENU_WIDTH - 8),
        );
        const below = window.innerHeight - rect.bottom;
        const top =
          below > MENU_MAX_HEIGHT / 2
            ? rect.bottom + 4
            : Math.max(8, rect.top - 4 - Math.min(MENU_MAX_HEIGHT, rect.top - 12));
        node.style.left = `${left}px`;
        node.style.top = `${top}px`;
      }
      node.style.visibility = "visible";
      node.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    },
    [trigger],
  );

  useEffect(() => {
    const dismiss = () => onClose(false);
    // Scrolling *inside* the menu's own list must not dismiss it — on touch
    // that would make options past the first screenful unreachable.
    const dismissOnOutsideScroll = (event: Event) => {
      if (
        menuRef.current &&
        event.target instanceof Node &&
        menuRef.current.contains(event.target)
      ) {
        return;
      }
      onClose(false);
    };
    window.addEventListener("resize", dismiss);
    window.addEventListener("scroll", dismissOnOutsideScroll, true);
    return () => {
      window.removeEventListener("resize", dismiss);
      window.removeEventListener("scroll", dismissOnOutsideScroll, true);
    };
  }, [onClose]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose(true);
      return;
    }
    if (event.key === "Tab") {
      onClose(false);
      return;
    }
    const all = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'),
    );
    if (all.length === 0) return;
    const index = all.indexOf(document.activeElement as HTMLButtonElement);
    const focus = (next: number) => {
      event.preventDefault();
      all[(next + all.length) % all.length].focus();
    };
    if (event.key === "ArrowDown") focus(index + 1);
    else if (event.key === "ArrowUp") focus(index - 1);
    else if (event.key === "Home") focus(0);
    else if (event.key === "End") focus(all.length - 1);
  };

  const content = (
    <>
      {/* Click-away catcher. Pointer-only, so it adds nothing for keyboards. */}
      <div className="fixed inset-0 z-40" onMouseDown={() => onClose(false)} aria-hidden />
      <div
        ref={placeMenu}
        id={id}
        role="menu"
        aria-label={`Move ${agent.name}`}
        onKeyDown={onKeyDown}
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) onClose(false);
        }}
        style={{ position: "fixed", top: 0, left: 0, width: MENU_WIDTH, visibility: "hidden" }}
        className="z-50 max-h-80 overflow-y-auto rounded-xl border border-line bg-surface p-1.5 shadow-card"
      >
        {groups.map((group) => (
          <div key={group.label} role="group" aria-label={group.label} className="py-1">
            <p className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-faint">
              {group.label}
            </p>
            {group.options.length === 0 ? (
              <p className="px-2 py-1 text-xs text-faint">No one else yet.</p>
            ) : null}
            {group.options.map((option) => (
              <button
                key={option.id}
                type="button"
                role="menuitem"
                aria-disabled={!option.check.ok || undefined}
                aria-current={option.current || undefined}
                onClick={() => {
                  if (option.check.ok) onPick(option.target);
                }}
                className={`flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left text-[13px] transition-colors ${focusRing} ${
                  option.check.ok ? "text-ink hover:bg-hover" : "cursor-not-allowed text-faint"
                }`}
              >
                <span className="mt-0.5 w-3.5 shrink-0">
                  {option.current ? (
                    <Check size={13} aria-hidden />
                  ) : !option.check.ok ? (
                    <Ban size={13} aria-hidden />
                  ) : null}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{option.label}</span>
                  {option.current ? (
                    <span className="block text-[11px] text-faint">Where they are now</span>
                  ) : option.check.reason ? (
                    <span className="block text-[11px] text-warn">{option.check.reason}</span>
                  ) : null}
                </span>
              </button>
            ))}
          </div>
        ))}
      </div>
    </>
  );

  if (typeof document === "undefined") return content;
  return createPortal(content, document.body);
}
