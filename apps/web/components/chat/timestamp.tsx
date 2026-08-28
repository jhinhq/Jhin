/** The transcript's relative time stamp ("4m", "2h"), with the absolute time
 * on hover. Shared by every card in the thread so they all age the same way. */

import { relativeTime } from "@/lib/chat";
import { formatDateTime } from "@/lib/format";

export function Timestamp({ iso, className = "" }: { iso: string; className?: string }) {
  return (
    <time dateTime={iso} title={formatDateTime(iso)} className={`text-[11px] text-faint ${className}`}>
      {relativeTime(iso)}
    </time>
  );
}
