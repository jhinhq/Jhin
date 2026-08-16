"use client";

/** Shared centered card layout for the login / first-run pages. */

export function AuthCard({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 flex h-11 w-11 items-center justify-center rounded-xl border border-accent/40 bg-accent-soft text-lg font-bold text-accent-strong">
            J
          </div>
          <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
          <p className="mt-1 text-sm text-dim">{subtitle}</p>
        </div>
        <div className="rounded-xl border border-line bg-surface p-6 shadow-xl shadow-black/30">
          {children}
        </div>
      </div>
    </main>
  );
}
