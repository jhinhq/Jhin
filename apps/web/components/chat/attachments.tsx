"use client";
/* eslint-disable @next/next/no-img-element -- Authenticated images must use the browser's session, not an image proxy. */
import { Paperclip, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiUploadProgress, errorText } from "@/lib/api";
import { fileSize, type ManagedFile } from "@/lib/workspace-files";

interface Upload { id: string; name: string; progress: number | null; status: "uploading" | "processing" | "failed"; error?: string; controller: AbortController; }
export function useAttachments(workspaceId: string, conversationId: string | null, onUploaded: (file: ManagedFile) => void) {
  const [uploads, setUploads] = useState<Upload[]>([]);
  const active = useRef(new Set<AbortController>());
  const queryClient = useQueryClient();
  useEffect(() => { const controllers = active.current; return () => controllers.forEach((controller) => controller.abort()); }, []);
  const addFiles = async (files: FileList | File[]) => {
    if (!conversationId) return;
    for (const file of Array.from(files)) {
      const id = crypto.randomUUID(), controller = new AbortController();
      if (file.size > 25 * 1024 * 1024) { setUploads((old) => [...old, { id, name: file.name, progress: 0, status: "failed", error: "Maximum file size is 25 MB.", controller }]); continue; }
      active.current.add(controller);
      setUploads((old) => [...old, { id, name: file.name, progress: 0, status: "uploading", controller }]);
      const form = new FormData(); form.append("file", file);
      try {
        const result = await apiUploadProgress<ManagedFile>(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/files`, form, controller.signal, (progress) => setUploads((old) => old.map((item) => item.id === id ? { ...item, progress, status: progress === 100 ? "processing" : "uploading" } : item)));
        if (result.status !== "ready") throw new Error(result.error || "This file could not be processed.");
        onUploaded(result); setUploads((old) => old.filter((item) => item.id !== id));
        void queryClient.invalidateQueries({ queryKey: ["conversation-files", workspaceId, conversationId] });
      } catch (error) {
        if (controller.signal.aborted) setUploads((old) => old.filter((item) => item.id !== id));
        else setUploads((old) => old.map((item) => item.id === id ? { ...item, status: "failed", error: errorText(error, error instanceof Error ? error.message : "Upload failed.") } : item));
      } finally { active.current.delete(controller); }
    }
  };
  const cancel = (id: string) => { uploads.find((item) => item.id === id)?.controller.abort(); setUploads((old) => old.filter((item) => item.id !== id)); };
  return { uploads, addFiles, cancel, busy: uploads.some((item) => item.status !== "failed") };
}

export function AttachmentTray({ files, uploads, onRemove, onCancel, revisionIds = {} }: { files: ManagedFile[]; uploads: Upload[]; onRemove: (id: string) => void; onCancel: (id: string) => void; revisionIds?: Record<string, string | undefined> }) {
  if (!files.length && !uploads.length) return null;
  return <ul className="flex flex-wrap gap-2 px-3 pt-3" aria-label="Message attachments">
    {files.map((file) => <li key={file.id} className="flex max-w-full items-center gap-2 rounded-lg border border-line bg-raised px-2 py-1.5 text-xs">
      {file.preview_kind === "image" ? <img alt="" src={revisionIds[file.id] ? `${file.preview_url}${file.preview_url.includes("?") ? "&" : "?"}revision_id=${encodeURIComponent(revisionIds[file.id]!)}` : file.preview_url} className="h-9 w-9 rounded object-cover" /> : <Paperclip size={14} />}
      <span className="min-w-0"><span className="block truncate font-medium">{file.name}</span><span className="text-faint">{revisionIds[file.id] && revisionIds[file.id] !== file.current_revision_id ? "Selected earlier version" : `v${file.version} · ${fileSize(file.size_bytes)}`} · Ready</span></span><button type="button" aria-label={`Remove ${file.name}`} className="min-h-8 min-w-8" onClick={() => onRemove(file.id)}><X size={14} /></button>
    </li>)}
    {uploads.map((item) => <li key={item.id} className="flex max-w-full items-center gap-2 rounded-lg border border-line p-2 text-xs"><span className="min-w-0"><span className="block truncate">{item.name}</span><span role={item.error ? "alert" : "status"} className={item.error ? "text-danger" : "text-dim"}>{item.error || (item.status === "processing" ? "Processing…" : `Uploading${item.progress === null ? "…" : ` ${item.progress}%`}`)}</span></span><button type="button" aria-label={`Cancel ${item.name}`} className="min-h-8 min-w-8" onClick={() => onCancel(item.id)}><X size={14} /></button></li>)}
  </ul>;
}

export function AttachButton({ onFiles, disabled }: { onFiles: (files: FileList) => void; disabled?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  return <><input ref={input} type="file" multiple className="hidden" onChange={(event) => { if (event.target.files) onFiles(event.target.files); event.target.value = ""; }} /><button type="button" title="Attach files" aria-label="Attach files" disabled={disabled} onClick={() => input.current?.click()} className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-dim hover:bg-hover disabled:opacity-40"><Paperclip size={17} /></button></>;
}
