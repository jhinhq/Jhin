"use client";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, errorText } from "@/lib/api";
import { conversationPath, type ConversationRuntime, type RuntimeSession } from "@/lib/workspace-runtime";
import { ErrorNote } from "@/components/ui";
import type { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

function TerminalDisplay({ workspaceId, conversationId, session, writable }: { workspaceId: string; conversationId: string; session: RuntimeSession; writable: boolean }) {
  const node = useRef<HTMLDivElement>(null);
  const [connection, setConnection] = useState("Connecting…");
  const [error, setError] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const allowInput = useRef(writable);
  useEffect(() => { allowInput.current = writable; }, [writable]);
  useEffect(() => {
    let disposed = false, terminal: Terminal | undefined, socket: WebSocket | undefined, observer: ResizeObserver | undefined, retry: ReturnType<typeof setTimeout> | undefined;
    let offset = Number.isSafeInteger(session.output_offset) ? session.output_offset : 0, sequence = 0;
    let ended = ["completed", "closed", "failed", "stopped", "cancelled", "expired"].includes(session.status);
    const pending = new Set<number>();
    const run = async () => {
      const [{ Terminal: Xterm }, { FitAddon }] = await Promise.all([import("@xterm/xterm"), import("@xterm/addon-fit")]);
      if (disposed || !node.current) return;
      terminal = new Xterm({ cursorBlink: true, convertEol: true, scrollback: 5000, screenReaderMode: true, fontSize: 12, fontFamily: "monospace", theme: { background: "#15141b", foreground: "#eeeaf8" } });
      const fit = new FitAddon(); terminal.loadAddon(fit); terminal.open(node.current); fit.fit();
      if (session.output) terminal.write(session.output);
      terminal.onData((data) => {
        if (!allowInput.current || socket?.readyState !== WebSocket.OPEN) return;
        const seq = ++sequence; pending.add(seq); socket.send(JSON.stringify({ type: "input", data, seq }));
      });
      observer = new ResizeObserver(() => { fit.fit(); if (allowInput.current && socket?.readyState === WebSocket.OPEN && terminal) socket.send(JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows })); });
      observer.observe(node.current);
      const connect = async () => {
        try {
          const ticket = await api<{ ticket: string; websocket_url: string }>(`${conversationPath(workspaceId, conversationId)}/terminals/${session.id}/ticket`, { method: "POST" });
          if (disposed) return;
          const url = new URL(ticket.websocket_url, window.location.href);
          if (url.origin !== window.location.origin) throw new Error("Invalid terminal gateway");
          url.protocol = url.protocol === "https:" ? "wss:" : "ws:"; url.searchParams.set("after", String(offset));
          socket = new WebSocket(url, ["jhin-session", ticket.ticket]);
          socket.onopen = () => { if (disposed) return; setConnection("Connected"); if (terminal && allowInput.current) socket?.send(JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows })); };
          socket.onmessage = (event) => {
            try {
              const message = JSON.parse(String(event.data)) as { type: string; data?: string; offset?: number; output_offset?: number; status?: string; exit_code?: number; seq?: number; message?: string; truncated?: boolean };
              if (message.truncated) setTruncated(true);
              if (message.type === "output" && typeof message.data === "string") {
                if (typeof message.offset !== "number" || !Number.isSafeInteger(message.offset) || message.offset <= offset) return;
                terminal?.write(message.data); offset = message.offset;
              } else if (message.type === "ack" && typeof message.seq === "number") pending.delete(message.seq);
              else if (message.type === "status") { ended = ["completed", "closed", "failed", "stopped", "cancelled", "expired"].includes(message.status ?? ""); setConnection(`${message.status ?? ""}${message.exit_code !== null && message.exit_code !== undefined ? ` · Exit ${message.exit_code}` : ""}`); }
              else if (message.type === "error") setError(message.message ?? "Terminal error");
            } catch { setError("An invalid terminal update was ignored."); }
          };
          socket.onclose = () => {
            if (disposed || ended) return;
            if (pending.size) { setError("The connection dropped after input was sent. Check the terminal before entering it again."); pending.clear(); }
            setConnection("Reconnecting…"); retry = setTimeout(() => { void connect(); }, 2000);
          };
          socket.onerror = () => { if (!disposed) setConnection("Connection interrupted"); };
        } catch (failure) { if (!disposed) { setError(errorText(failure, "Couldn't connect to the terminal.")); setConnection("Disconnected"); } }
      };
      if (ended) setConnection(`${session.status}${session.exit_code != null ? ` · Exit ${session.exit_code}` : ""}`);
      else await connect();
    };
    void run().catch(() => setError("Couldn't load the terminal."));
    return () => { disposed = true; if (retry) clearTimeout(retry); socket?.close(); observer?.disconnect(); terminal?.dispose(); };
    // The terminal keeps its buffer between status polls; a new session creates a new emulator.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, conversationId, session.id]);
  return <div className="flex min-h-0 flex-1 flex-col gap-2"><div className="flex justify-between text-xs text-dim"><span role="status">{connection}</span><span>{writable ? "You control this terminal" : "Read only"}</span></div><ErrorNote message={error} />{truncated ? <p className="text-xs text-warn">Earlier output expired while disconnected. The retained output follows.</p> : null}<div ref={node} aria-label="Interactive terminal" className="min-h-72 flex-1 overflow-hidden rounded-lg bg-[#15141b] p-2" /><p className="text-[11px] text-faint">Terminal scrollback keeps the latest 5,000 lines; reconnect replay is bounded.</p></div>;
}

export function WorkspaceTerminal({ workspaceId, conversationId, runtime, isAdmin, userId }: { workspaceId: string; conversationId: string; runtime: ConversationRuntime | undefined; isAdmin: boolean; userId: string }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null), [network, setNetwork] = useState("none");
  const owns = runtime?.owner === "user" && runtime.owner_user_id === userId;
  const terminalActive = runtime?.terminal && ["starting", "running", "ready"].includes(runtime.terminal.status);
  const change = async (suffix: string, body?: unknown) => {
    setBusy(true); setError(null);
    try { await api(`${conversationPath(workspaceId, conversationId)}${suffix}`, { method: "POST", body }); await queryClient.invalidateQueries({ queryKey: ["conversation-runtime", workspaceId, conversationId] }); }
    catch (failure) { setError(errorText(failure, "Couldn't change terminal state.")); }
    finally { setBusy(false); }
  };
  return <div className="flex h-full min-h-0 flex-col gap-3 p-3"><p className="break-all font-mono text-xs text-dim">{runtime?.cwd ?? "/workspace"}</p><div className="flex flex-wrap items-center gap-2 text-xs">
    {isAdmin ? <button type="button" disabled={busy || !runtime} onClick={() => void change("/runtime/control", { action: owns ? "return" : "take" })} className="rounded-lg border border-line px-3 py-2 disabled:opacity-40">{owns ? "Return to agent" : "Take control"}</button> : <span className="text-dim">Only owners and admins can control terminals.</span>}
    {!terminalActive && owns ? <><select aria-label="Terminal network access" value={network} onChange={(event) => setNetwork(event.target.value)} className="rounded border border-line bg-surface p-2"><option value="none">Internet blocked</option><option value="internet">Internet enabled</option></select><button type="button" disabled={busy} className="rounded-lg bg-accent px-3 py-2 text-white" onClick={() => void change("/terminals", { network })}>{runtime?.terminal ? "New terminal" : "Open terminal"}</button></> : null}
    {runtime?.terminal && terminalActive && owns ? <><button type="button" disabled={busy} className="rounded-lg border border-line px-3 py-2" onClick={() => void change(`/terminals/${runtime.terminal!.id}/interrupt`)}>Interrupt</button><button type="button" disabled={busy} className="rounded-lg border border-line px-3 py-2" onClick={() => void change(`/terminals/${runtime.terminal!.id}/close`)}>Close terminal</button></> : null}
  </div><ErrorNote message={error} />{runtime?.terminal ? <><p className="text-xs text-dim">Terminal opened by {runtime.terminal.user_id === userId ? "you" : "a workspace member"} · {runtime.terminal.network === "internet" ? "Internet enabled" : "Internet blocked"}</p><TerminalDisplay key={`${runtime.terminal.id}:${runtime.lease_generation}:${terminalActive ? "active" : "ended"}`} workspaceId={workspaceId} conversationId={conversationId} session={runtime.terminal} writable={!!terminalActive && isAdmin && owns} /></> : <p className="py-8 text-center text-sm text-dim">Take control to start an interactive shell in this chat’s workspace.</p>}</div>;
}
