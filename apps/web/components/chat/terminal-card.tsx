"use client";

import { ChevronDown, ChevronRight, Terminal } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { Timestamp } from "@/components/chat/timestamp";
import type { ConversationToolCall } from "@/lib/types";

const OUTPUT_LIMIT = 8192;
const labels: Record<string, string> = {
  "cli.command.execute": "Terminal", "cli.test.run": "Terminal",
  "cli.file.read": "Read file", "cli.file.list": "List files", "cli.file.search": "Search files",
  "cli.file.write": "Write file", "cli.file.edit": "Edit file",
  "cli.repository.checkout": "Check out repository", "cli.repository.push": "Push repository",
};
const text = (value: unknown): string => typeof value === "string" ? value : "";

export function TerminalCard({ call, initialOpen = true }: { call: ConversationToolCall; initialOpen?: boolean }) {
  const [open, setOpen] = useState(initialOpen);
  const outputId = useId();
  const outputRef = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const nested = call.sanitized_input_json.input;
  const input = nested && typeof nested === "object" && !Array.isArray(nested)
    ? nested as Record<string, unknown> : call.sanitized_input_json;
  const result = call.sanitized_output_json;
  const note = (text(result.reason) || text(result.message) || text(result.error) || call.error_code || "").slice(0, OUTPUT_LIMIT);
  const job = call.sandbox_job;
  const commandTool = ["cli.command.execute", "cli.test.run"].includes(call.tool_name);
  const command = commandTool ? text(input.command) || text(result.command) : text(input.path) || text(input.repository) || text(input.pattern);
  const stdout = (job ? job.stdout : text(result.stdout)).slice(-OUTPUT_LIMIT);
  const stderr = (job ? job.stderr : text(result.stderr)).slice(-OUTPUT_LIMIT);
  const content = !commandTool && !stdout && !stderr && Object.keys(result).length
    ? (text(result.content) || JSON.stringify(result, null, 2)).slice(0, OUTPUT_LIMIT) : "";
  const exitCode = job?.exit_code ?? (typeof result.exit_code === "number" ? result.exit_code : null);
  const status = job?.status || text(result.status) || call.status;
  const failed = ["failed", "timeout", "cancelled"].includes(status)
    || ["failed", "denied", "rejected", "execution_unknown"].includes(call.status)
    || (exitCode !== null && exitCode !== 0);
  const done = failed || ["completed", "executed"].includes(status) || call.status === "executed";
  const waiting = ["needs_approval", "needs_review", "pending_approval", "pending_review"].includes(call.status);
  const state = call.status === "execution_unknown" ? "Outcome unknown"
    : ["denied", "rejected"].includes(call.status) ? "Not allowed"
    : status === "timeout" ? "Timed out" : status === "cancelled" ? "Cancelled"
    : failed ? "Failed" : done ? "Completed"
    : ["pending_review", "needs_review"].includes(call.status) ? "Needs review" : waiting ? "Needs approval"
    : job?.status === "running" || stdout || stderr ? "Running" : "Starting";
  const duration = job?.duration_ms ?? call.duration_ms;

  useEffect(() => {
    if (open && following.current && outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [stdout, stderr, content, open]);

  return (
    <article data-testid="terminal-call" data-call-id={call.id} className="min-w-0 overflow-hidden rounded-xl border border-line bg-surface shadow-[var(--card-shadow)]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-3 py-2 text-xs">
        <span className="flex min-w-0 items-center gap-2 font-medium text-ink"><Terminal size={14} aria-hidden />{labels[call.tool_name] ?? "Workspace operation"}</span>
        <span className="truncate text-dim">{call.agent_name ?? "Agent"}</span>
        <span className={`ml-auto inline-flex items-center gap-1.5 ${failed ? "text-danger" : "text-dim"}`}>
          {!done && !waiting ? <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-accent motion-safe:animate-pulse" /> : null}
          {state}
        </span>
      </div>
      {command ? <pre className="whitespace-pre-wrap break-all px-3 py-3 font-mono text-xs leading-relaxed text-ink"><code>{command}</code></pre> : null}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 pb-2 text-[11px] text-faint">
        <Timestamp iso={call.created_at} />
        {exitCode !== null ? <span>Exit {exitCode}</span> : null}
        {duration !== null && duration !== undefined ? <span>{(duration / 1000).toFixed(1)}s</span> : null}
        {job ? <span>{job.network_policy === "internet" ? "Internet enabled" : "Internet blocked"}</span> : null}
        <button type="button" aria-expanded={open} aria-controls={outputId} onClick={() => setOpen(!open)} className="ml-auto inline-flex min-h-8 items-center gap-1 rounded px-1 text-dim hover:bg-hover focus-visible:outline-2 focus-visible:outline-accent">
          {open ? <ChevronDown size={13} aria-hidden /> : <ChevronRight size={13} aria-hidden />}
          {open ? "Hide output" : "Show output"}
        </button>
      </div>
      {open ? <div id={outputId} aria-live="off" className="border-t border-line bg-raised">
        {note ? <p className="whitespace-pre-wrap break-words px-3 pt-3 text-xs text-dim">{note}</p> : null}
        {stdout || stderr || content ? <>
          <div ref={outputRef} onScroll={() => {
            const node = outputRef.current;
            if (node) following.current = node.scrollHeight - node.scrollTop - node.clientHeight < 32;
          }} className="max-h-64 overflow-auto p-3 font-mono text-xs leading-relaxed">
            {stdout ? <pre aria-label="Standard output" className="whitespace-pre-wrap break-all text-ink">{stdout}</pre> : null}
            {stderr ? <div className={stdout ? "mt-3 border-t border-line pt-2" : ""}><span className="mb-1 block font-sans text-[11px] text-dim">Standard error</span><pre aria-label="Standard error" className="whitespace-pre-wrap break-all text-ink">{stderr}</pre></div> : null}
            {content ? <pre aria-label="Result" className="whitespace-pre-wrap break-all text-ink">{content}</pre> : null}
          </div>
          <p className="border-t border-line px-3 py-1.5 text-[10px] text-faint">{content ? "Result preview" : "Latest output"} · up to 8,192 characters{content ? "" : " per stream"}</p>
        </> : <p className="px-3 py-3 text-xs text-dim">{done ? "No output recorded." : waiting ? "Execution is waiting for review." : "Waiting for output…"}</p>}
      </div> : null}
    </article>
  );
}
