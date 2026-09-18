"use client";
import { ChevronDown, ChevronRight, Wrench } from "lucide-react";
import { useState } from "react";
import type { ActivityDetail } from "@/lib/agentic-chat";
import type { ConversationToolCall } from "@/lib/types";
import { TerminalCard } from "./terminal-card";

export function ActionCard({ call, detail = "standard", logsUrl }: { call: ConversationToolCall; detail?: ActivityDetail; logsUrl?: string }) {
  const failed = ["failed", "denied", "rejected", "execution_unknown"].includes(call.status);
  const complete = ["executed", "completed"].includes(call.status);
  const [open, setOpen] = useState(detail === "detailed" || failed);
  if (call.tool_name.startsWith("cli.")) return <div><TerminalCard key={`${call.id}:${detail}`} call={call} initialOpen={detail !== "compact" || failed} />{logsUrl ? <a className="ml-2 text-[11px] text-dim underline" href={logsUrl} download>Download retained log</a> : null}</div>;
  const output = call.sanitized_output_json;
  const summary = [output.summary, output.message, output.error].find((value) => typeof value === "string") as string | undefined;
  const label = call.tool_name.replace(/(?:^mcp\.|__)/g, " ").replace(/[._]/g, " ").trim();
  return <article className={`min-w-0 overflow-hidden rounded-xl border ${failed ? "border-danger/30" : "border-line"} bg-surface`} data-testid="action-call" data-call-id={call.id}>
    <button type="button" className="flex min-h-11 w-full items-center gap-2 px-3 py-2 text-left text-xs" aria-expanded={open} onClick={() => setOpen(!open)}>
      <Wrench size={14} aria-hidden /><span className="min-w-0 flex-1"><span className="block font-medium capitalize text-ink">{label}</span><span className="text-faint">{call.agent_name ?? "Agent"}</span></span>
      <span className={failed ? "text-danger" : "text-dim"}>{failed ? "Failed" : complete ? "Completed" : call.status.replaceAll("_", " ")}</span>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
    </button>
    {summary && (detail !== "compact" || failed) ? <p className="px-3 pb-3 text-sm text-dim">{summary.slice(0, 2000)}</p> : null}
    {open ? <div className="space-y-3 border-t border-line bg-raised p-3 text-xs"><details open={detail === "detailed"}><summary className="cursor-pointer text-dim">Input</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all pt-2">{JSON.stringify(call.sanitized_input_json, null, 2)}</pre></details><p className="font-medium">Result</p><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all">{Object.keys(output).length ? JSON.stringify(output, null, 2) : complete ? "No result recorded." : "Waiting for result…"}</pre></div> : null}
  </article>;
}
