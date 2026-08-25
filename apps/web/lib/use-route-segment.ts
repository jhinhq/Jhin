"use client";

/** The hook half of `lib/route-params.ts` — see there for why pages read the
 * pathname rather than `useParams()`. */

import { usePathname } from "next/navigation";
import { segmentAfter } from "@/lib/route-params";

export function useSegmentAfter(prefix: string): string {
  return segmentAfter(usePathname(), prefix);
}
