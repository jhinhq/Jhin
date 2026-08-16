const STYLES: Record<string, string> = {
  ok: "bg-emerald-500/15 text-emerald-400 ring-emerald-500/30",
  degraded: "bg-amber-500/15 text-amber-400 ring-amber-500/30",
  error: "bg-red-500/15 text-red-400 ring-red-500/30",
  unreachable: "bg-red-500/15 text-red-400 ring-red-500/30",
};

export function StatusBadge({ status }: { status: string }) {
  const style = STYLES[status] ?? STYLES.error;
  return (
    <span
      data-testid="status-badge"
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${style}`}
    >
      <span className="size-1.5 rounded-full bg-current" aria-hidden />
      {status}
    </span>
  );
}
