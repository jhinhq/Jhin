"use client";

/** The one filter dimension that stays visible: a single scrolling row of
 * category pills with "All" first. Everything subtler lives behind "More
 * filters"; this rail is the whole default filtering surface.
 *
 * The row scrolls sideways, and a row that scrolls must say so: a soft fade
 * hangs over whichever edge still has pills hiding past it, so a chip cut
 * mid-word reads as "more this way" instead of a rendering bug. */

import { useCallback, useEffect, useRef, useState } from "react";
import { focusRing } from "@/components/ui";

const pillClass = (active: boolean) =>
  `inline-flex min-h-10 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-4 text-sm font-medium transition-colors ${focusRing} ${
    active
      ? "border-line-strong bg-accent-soft text-accent-strong"
      : "border-line bg-surface text-dim hover:text-ink"
  }`;

export function CategoryRail({
  categories,
  active,
  onChange,
}: {
  categories: { value: string; label: string; count?: number }[];
  active: string | null;
  onChange: (value: string | null) => void;
}) {
  const railRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState({ left: false, right: false });

  const measure = useCallback(() => {
    const rail = railRef.current;
    if (!rail) return;
    const left = rail.scrollLeft > 4;
    const right = rail.scrollLeft + rail.clientWidth < rail.scrollWidth - 4;
    setOverflow((current) =>
      current.left === left && current.right === right ? current : { left, right },
    );
  }, []);

  useEffect(() => {
    measure();
    const rail = railRef.current;
    if (!rail) return;
    rail.addEventListener("scroll", measure, { passive: true });
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    observer?.observe(rail);
    return () => {
      rail.removeEventListener("scroll", measure);
      observer?.disconnect();
    };
  }, [measure, categories.length]);

  return (
    <div className="relative">
      <div
        ref={railRef}
        className="flex gap-2 overflow-x-auto pb-1"
        role="group"
        aria-label="Category"
        data-testid="category-rail"
      >
        <button
          type="button"
          aria-pressed={active === null}
          onClick={() => onChange(null)}
          className={pillClass(active === null)}
        >
          All
        </button>
        {categories.map((category) => {
          const isActive = active === category.value;
          return (
            <button
              key={category.value}
              type="button"
              aria-pressed={isActive}
              onClick={() => onChange(isActive ? null : category.value)}
              className={pillClass(isActive)}
            >
              {category.label}
              {category.count !== undefined ? (
                <span className="text-faint">{category.count}</span>
              ) : null}
            </button>
          );
        })}
      </div>
      {overflow.left ? (
        <div
          aria-hidden
          data-testid="category-rail-fade-left"
          className="pointer-events-none absolute inset-y-0 left-0 w-10 bg-linear-to-r from-bg to-transparent"
        />
      ) : null}
      {overflow.right ? (
        <div
          aria-hidden
          data-testid="category-rail-fade-right"
          className="pointer-events-none absolute inset-y-0 right-0 w-10 bg-linear-to-l from-bg to-transparent"
        />
      ) : null}
    </div>
  );
}
