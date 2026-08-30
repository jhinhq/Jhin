"use client";

/** Chat shell: rail on the left, the selected thread (or home) on the right.
 * On small screens the rail is the whole screen until a chat or "new chat"
 * is opened. */

import { useParams, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { ChatRail } from "@/components/chat/chat-rail";

function ChatsFrame({ children }: { children: React.ReactNode }) {
  const params = useParams<{ id?: string }>();
  const search = useSearchParams();
  const selectedId = typeof params?.id === "string" ? params.id : null;
  // ?new=1 is the rail's "new chat"; ?agent=<id> is an agent deep link into
  // the chats home. Both open the main pane on small screens.
  const showMainOnMobile =
    selectedId !== null || search.get("new") === "1" || search.get("agent") !== null;

  // Mobile: the shell adds a 3.5rem top bar and a fixed bottom tab bar of
  // 3.5rem plus the home-indicator inset.
  return (
    <div className="flex h-[calc(100dvh-7rem-env(safe-area-inset-bottom))] min-h-0 w-full overflow-hidden bg-bg md:h-dvh">
      <aside
        className={`${showMainOnMobile ? "hidden lg:flex" : "flex"} w-full shrink-0 flex-col border-line bg-surface lg:w-[300px] lg:border-r`}
      >
        <ChatRail selectedId={selectedId} />
      </aside>
      <div className={`${showMainOnMobile ? "flex" : "hidden lg:flex"} min-w-0 flex-1 flex-col`}>
        {children}
      </div>
    </div>
  );
}

export default function ChatsLayout({ children }: { children: React.ReactNode }) {
  return (
    <Suspense fallback={<div className="h-[calc(100dvh-7rem-env(safe-area-inset-bottom))] md:h-dvh" />}>
      <ChatsFrame>{children}</ChatsFrame>
    </Suspense>
  );
}
