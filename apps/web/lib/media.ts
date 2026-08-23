/** Pure helpers for avatars: URL sizing, upload validation, spreadable
 * `Avatar` props, and the friendly wording for API rejection codes. */

import type { AgentAvatar, AvatarVariantSize } from "@/lib/types";

interface AvatarVisualProps {
  src: string | null;
  shape: string | null;
  color: string | null;
}

/** Spreadable `Avatar` props from an avatar-map entry (`useAgentAvatarMap`). */
export function avatarProps(entry?: AgentAvatar | null): AvatarVisualProps {
  return { src: entry?.url ?? null, shape: entry?.shape ?? null, color: entry?.color ?? null };
}

/** Spreadable `Avatar` props straight from an agent-shaped payload. */
export function identityAvatarProps(agent: {
  avatar_url?: string | null;
  avatar_shape?: string | null;
  avatar_color?: string | null;
}): AvatarVisualProps {
  return {
    src: agent.avatar_url ?? null,
    shape: agent.avatar_shape ?? null,
    color: agent.avatar_color ?? null,
  };
}

const AVATAR_MAX_BYTES = 8 * 1024 * 1024;
const AVATAR_ACCEPTED_TYPES = ["image/png", "image/jpeg", "image/webp"] as const;

/** Pixel variant to request for each `Avatar` size (devicePixelRatio-safe). */
export const AVATAR_VARIANT_FOR_SIZE: Record<"xs" | "sm" | "md" | "lg" | "xl", AvatarVariantSize> = {
  xs: 64,
  sm: 64,
  md: 128,
  lg: 128,
  xl: 256,
};

/** `avatar_url` + `?size=`; null/empty stays null so callers fall back to
 * initials. Existing query strings are preserved. */
export function avatarSrc(url: string | null | undefined, size: AvatarVariantSize): string | null {
  if (!url) return null;
  const joiner = url.includes("?") ? "&" : "?";
  return `${url}${joiner}size=${size}`;
}

/** Client-side pre-check so obviously wrong files never leave the browser. */
export function validateAvatarFile(file: Pick<File, "type" | "size" | "name">): string | null {
  if (!(AVATAR_ACCEPTED_TYPES as readonly string[]).includes(file.type)) {
    return "Use a PNG, JPEG, or WebP image. Animated images and SVGs aren't supported.";
  }
  if (file.size > AVATAR_MAX_BYTES) {
    return `That image is ${(file.size / (1024 * 1024)).toFixed(1)} MB; the limit is 8 MB. Try a smaller export.`;
  }
  if (file.size === 0) return "That file is empty. Pick another image.";
  return null;
}

/** Plain-language wording for `{code}` from the upload/generate routes. */
export function avatarErrorMessage(code: string | null, fallback: string): string {
  switch (code) {
    case "too_large":
      return "That image is over 8 MB. Try a smaller export.";
    case "unsupported_format":
      return "We can only use PNG, JPEG, or WebP images. SVGs, GIFs, and videos won't work.";
    case "content_type_mismatch":
      return "The file doesn't match its type (for example a PNG renamed to .jpg). Re-export it and try again.";
    case "animated":
      return "Animated images aren't supported. Pick a still image.";
    case "too_many_pixels":
    case "decompression_bomb":
      return "That image is too big (over 4096 × 4096). Resize it and try again.";
    case "too_small":
      return "That image is too small; use one at least 16 pixels on each side.";
    case "undecodable":
    case "empty":
      return "We couldn't read that image. It may be corrupt or truncated.";
    case "image_generation_unsupported":
      return "No model here can draw pictures yet. An admin can enable image generation on a model profile under Advanced → Models.";
    case "generation_in_progress":
      return "An illustration is already being made for this agent. Give it a moment.";
    case "provider_disabled":
      return "The image provider is turned off. Turn it back on under Advanced → Models, or upload a picture instead.";
    case "provider_config":
      return "The image provider isn't set up correctly. Check it under Advanced → Models.";
    case "provider_error":
      return "The image provider had a problem. The previous avatar is unchanged; try again in a minute.";
    case "image_rejected":
      return "The generated picture didn't pass our checks, so nothing changed. Try again or add a hint.";
    case "dispatch_failed":
      return "We couldn't start the generation. Nothing changed; try again in a moment.";
    case "agent_not_found":
    case "generation_not_found":
      return "That agent or generation no longer exists.";
    default:
      return fallback;
  }
}

/** "$0.04 per image" or "cost not listed". */
export function formatDisclosureCost(micros: number | null): string {
  if (micros === null) return "cost not listed by the operator";
  return `about $${(micros / 1_000_000).toFixed(micros < 10_000 ? 4 : 2)} per image`;
}
