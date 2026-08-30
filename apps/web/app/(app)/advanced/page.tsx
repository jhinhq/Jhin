"use client";

/** Index of the operational ("Advanced") screens with plain-language notes. */

import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { ADVANCED_NAV, PageBody, PageHeader } from "@/components/app-shell";
import { focusRing } from "@/components/ui";

export default function AdvancedIndexPage() {
  return (
    <>
      <PageHeader
        eyebrow="Advanced"
        title="All advanced tools"
        description="These screens show the machinery behind your agents. You rarely need them day to day, but they are here when you want to look under the hood."
      />
      <PageBody>
        <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {ADVANCED_NAV.map((item) => {
            const Icon = item.icon;
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className={`group flex h-full flex-col gap-3 rounded-2xl border border-line bg-surface p-5 shadow-card transition-colors hover:border-accent ${focusRing}`}
                >
                  <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent-soft text-accent-strong">
                    <Icon size={18} strokeWidth={1.8} aria-hidden />
                  </span>
                  <span className="font-display text-base font-semibold text-ink">
                    {item.label}
                  </span>
                  <span className="flex-1 text-sm text-dim">{item.description}</span>
                  <span className="inline-flex items-center gap-1 text-[13px] font-medium text-accent-strong">
                    Open
                    <ArrowRight
                      size={14}
                      aria-hidden
                      className="transition-transform group-hover:translate-x-0.5"
                    />
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
        <p className="mt-8 max-w-2xl text-sm text-faint">
          Tip: everyday setup lives in the main navigation — Automations, Apps, Models, and
          Skills are all there. Attention gathers approvals and failures in one place; the
          screens here are the raw machinery behind those views.
        </p>
      </PageBody>
    </>
  );
}
