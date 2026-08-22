"use client";

/** Accessible avatar for agents and people. Renders the agent's picture when
 * `src` is given and falls back to initials when there is none or the image
 * fails to load. Always render next to a visible name; the avatar itself is
 * decorative (aria-hidden) unless a label is passed. Color is derived from
 * the name so an agent keeps the same hue everywhere. */

import { useState } from "react";
import { avatarSrc, AVATAR_VARIANT_FOR_SIZE } from "@/lib/media";

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

function hueIndex(name: string): number {
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

export type AvatarSize = keyof typeof SIZES;

export function Avatar({
  name,
  size = "md",
  kind = "agent",
  label,
  className = "",
  src,
}: {
  name: string;
  size?: AvatarSize;
  /** People get a neutral tint; agents get a brand hue. */
  kind?: "agent" | "user";
  label?: string;
  className?: string;
  /** The agent's `avatar_url` (relative media path) or a full image URL.
   * `?size=` is appended to match the rendered size. */
  src?: string | null;
}) {
  const resolved = src && src.includes("size=") ? src : avatarSrc(src, AVATAR_VARIANT_FOR_SIZE[size]);
  // Remember which source failed so a new source gets a fresh try.
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const failed = failedSrc !== null && failedSrc === resolved;

  const tone = kind === "user" ? "bg-raised text-dim border border-line" : HUES[hueIndex(name)];
  const shape = kind === "user" ? "rounded-full" : "rounded-xl";
  const showImage = Boolean(resolved) && !failed;
  return (
    <span
      aria-hidden={label ? undefined : true}
      aria-label={label}
      role={label ? "img" : undefined}
      data-avatar={showImage ? "image" : "initials"}
      className={`inline-flex shrink-0 select-none items-center justify-center overflow-hidden font-display font-semibold ${shape} ${SIZES[size]} ${showImage ? "bg-raised" : tone} ${className}`}
    >
      {showImage ? (
        // eslint-disable-next-line @next/next/no-img-element -- authenticated same-origin media; next/image can't proxy cookies
        <img
          src={resolved ?? undefined}
          alt=""
          draggable={false}
          className="h-full w-full object-cover"
          onError={() => setFailedSrc(resolved)}
        />
      ) : (
        initialsOf(name)
      )}
    </span>
  );
}
