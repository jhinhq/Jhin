"use client";
import { File } from "lucide-react";
import { fileSize, type ManagedFile } from "@/lib/workspace-files";
export function ArtifactCard({ file, onOpen, onUse }: { file: ManagedFile; onOpen: (file: ManagedFile) => void; onUse: (file: ManagedFile) => void }) {
  return <article className="overflow-hidden rounded-xl border border-line bg-surface" aria-label={`File: ${file.name}`}><button type="button" onClick={() => onOpen(file)} className="flex w-full items-center gap-3 p-3 text-left hover:bg-hover"><File size={22} className="shrink-0 text-accent" /><span className="min-w-0 flex-1"><span className="block truncate text-sm font-semibold">{file.name}</span><span className="text-xs text-dim">{file.mime_type} · {fileSize(file.size_bytes)} · v{file.version}</span></span><span className="text-xs text-accent-strong">Preview</span></button><div className="flex flex-wrap gap-3 border-t border-line px-3 py-2 text-xs"><a href={file.download_url} download className="text-accent-strong">Download</a><button type="button" onClick={() => onUse(file)} className="text-dim">Use in next message</button></div></article>;
}
