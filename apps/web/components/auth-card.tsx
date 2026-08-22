"use client";

/** Shared centered card layout for the login / first-run pages, with the
 * landing page's soft aurora backdrop. */

import { Wordmark } from "@/components/brand/logo-mark";
import { ThemeToggle } from "@/components/ui";

export function AuthCard({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden px-4 py-12">
      <div className="aurora" aria-hidden />
      <div className="absolute right-4 top-4 z-10">
        <ThemeToggle />
      </div>
      <div className="relative w-full max-w-[26rem]">
        <div className="mb-8 flex flex-col items-center text-center">
          <Wordmark className="mb-6 [&>svg]:h-12" />
          <h1 className="font-display text-2xl font-semibold tracking-tight">{title}</h1>
          <p className="mt-1.5 text-[15px] text-dim">{subtitle}</p>
        </div>
        <div className="rounded-2xl border border-line bg-surface p-6 shadow-card sm:p-8">
          {children}
        </div>
        {footer ? <div className="mt-6 text-center text-sm text-dim">{footer}</div> : null}
      </div>
    </main>
  );
}
