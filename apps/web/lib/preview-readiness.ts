"use client";
import { useEffect, useState } from "react";

export async function waitForPreview(url: string, signal: AbortSignal, timeoutMs = 240_000) {
  const deadline = Date.now() + timeoutMs;
  while (!signal.aborted) {
    const request = new AbortController();
    const abort = () => request.abort();
    signal.addEventListener("abort", abort, { once: true });
    const timeout = setTimeout(abort, 8_000);
    try {
      const response = await fetch(url, { method: "HEAD", cache: "no-store", signal: request.signal });
      if (response.status === 401 || response.status === 403) throw new Error("Preview access expired or was revoked. Reload the preview to reconnect.");
      // A reachable application may deliberately return an error or omit HEAD
      // support (501). Let its real GET render rather than hiding that response.
      if (response.status !== 502 && response.status !== 503) return;
    } catch (error) {
      if (signal.aborted) throw signal.reason ?? new Error("Preview opening cancelled");
      if (error instanceof Error && error.message.startsWith("Preview access")) throw error;
    } finally { clearTimeout(timeout); signal.removeEventListener("abort", abort); }
    if (Date.now() >= deadline) throw new Error("The preview is still starting. Check its logs, then reload the preview.");
    await new Promise<void>((resolve, reject) => {
      const abortWait = () => { clearTimeout(timer); reject(signal.reason ?? new Error("Preview opening cancelled")); };
      const timer = setTimeout(() => { signal.removeEventListener("abort", abortWait); resolve(); }, 2_000);
      signal.addEventListener("abort", abortWait, { once: true });
    });
  }
  throw signal.reason ?? new Error("Preview opening cancelled");
}

export function usePreviewReadiness(url: string | null) {
  const [result, setResult] = useState<{ url: string; error: string | null } | null>(null);
  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    void waitForPreview(url, controller.signal).then(() => {
      if (!controller.signal.aborted) setResult({ url, error: null });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setResult({ url, error: error instanceof Error ? error.message : "Couldn't open the preview." });
    });
    return () => controller.abort();
  }, [url]);
  const current = result?.url === url ? result : null;
  return { waiting: !!url && !current, ready: !!url && !!current && !current.error, error: current?.error ?? null };
}
