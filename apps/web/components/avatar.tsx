/** Accessible initials avatar for agents and people. Always render next to a
 * visible name; the avatar itself is decorative (aria-hidden) unless a label
 * is passed. Color is derived from the name so an agent keeps the same hue
 * everywhere. */

const HUES = [
  "bg-[#7371fc] text-white",
  "bg-[#a594f9] text-[#221e38]",
  "bg-[#5a58e8] text-white",
  "bg-[#cdc1ff] text-[#221e38]",
  "bg-[#2e7558] text-white",
  "bg-[#316f98] text-white",
  "bg-[#c96f43] text-white",
  "bg-[#985b08] text-white",
];

export function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export function hueIndex(name: string): number {
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return hash % HUES.length;
}

const SIZES = {
  xs: "h-6 w-6 text-[10px]",
  sm: "h-8 w-8 text-xs",
  md: "h-10 w-10 text-sm",
  lg: "h-14 w-14 text-lg",
  xl: "h-20 w-20 text-2xl",
} as const;

export function Avatar({
  name,
  size = "md",
  kind = "agent",
  label,
  className = "",
}: {
  name: string;
  size?: keyof typeof SIZES;
  /** People get a neutral tint; agents get a brand hue. */
  kind?: "agent" | "user";
  label?: string;
  className?: string;
}) {
  const tone = kind === "user" ? "bg-raised text-dim border border-line" : HUES[hueIndex(name)];
  const shape = kind === "user" ? "rounded-full" : "rounded-xl";
  return (
    <span
      aria-hidden={label ? undefined : true}
      aria-label={label}
      role={label ? "img" : undefined}
      className={`inline-flex shrink-0 select-none items-center justify-center font-display font-semibold ${shape} ${SIZES[size]} ${tone} ${className}`}
    >
      {initialsOf(name)}
    </span>
  );
}
