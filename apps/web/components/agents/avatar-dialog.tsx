"use client";

/** Admin "Change avatar" dialog: upload a picture, go back to initials, or
 * generate an illustration (with the provider disclosure shown before and
 * while it runs). Failures keep the previous avatar; the copy says so. */

import { useMutation } from "@tanstack/react-query";
import { ImagePlus, Sparkles, Trash2, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Avatar } from "@/components/avatar";
import { Button, Dialog, ErrorNote, Field, Input, Spinner, StatusLabel } from "@/components/ui";
import { api, ApiError, apiUpload } from "@/lib/api";
import { useAvatarGeneration, useInvalidateAvatar } from "@/lib/hooks";
import { avatarErrorMessage, formatDisclosureCost, validateAvatarFile } from "@/lib/media";
import type { Agent, AvatarGenerationOut, AvatarOut, ProviderDisclosure } from "@/lib/types";

type Mode = "menu" | "upload" | "generate";

function DisclosureNote({ disclosure }: { disclosure: ProviderDisclosure }) {
  return (
    <div className="rounded-xl border border-line bg-raised px-3.5 py-3 text-[13px] text-dim">
      <p>
        Drawn by <span className="font-medium text-ink">{disclosure.provider_display_name}</span> using{" "}
        <span className="font-medium text-ink">{disclosure.model_name}</span> ({disclosure.image_size}),{" "}
        {formatDisclosureCost(disclosure.estimated_cost_micros)}.
      </p>
      {disclosure.sends_public_identity ? (
        <p className="mt-1">
          Only the agent’s public identity (name, role, purpose, expertise) and your hint are sent. Instructions,
          memory, and chats never leave Jhin.
        </p>
      ) : null}
    </div>
  );
}

/** Mounted only while open so every opening starts with fresh state. */
export function AvatarDialog(props: { workspaceId: string; agent: Agent; open: boolean; onClose: () => void }) {
  if (!props.open) return null;
  return <AvatarDialogBody {...props} />;
}

function AvatarDialogBody({
  workspaceId,
  agent,
  onClose,
}: {
  workspaceId: string;
  agent: Agent;
  onClose: () => void;
}) {
  const [mode, setMode] = useState<Mode>("menu");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [hint, setHint] = useState("");
  const [tracking, setTracking] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const invalidatedFor = useRef<Set<string>>(new Set());
  const invalidate = useInvalidateAvatar(workspaceId, agent.id);
  const base = `/api/v1/workspaces/${workspaceId}/agents/${agent.id}/avatar`;

  const generation = useAvatarGeneration(workspaceId, agent.id, true);
  const latest = generation.data ?? null;
  const running = latest !== null && (latest.status === "queued" || latest.status === "running");

  const preview = useMemo(() => (file ? URL.createObjectURL(file) : null), [file]);
  useEffect(() => {
    return () => {
      if (preview) URL.revokeObjectURL(preview);
    };
  }, [preview]);

  // Outcome of the generation we started, derived from the polled row.
  const tracked = latest && tracking && latest.id === tracking ? latest : null;
  const generationSucceeded = tracked?.status === "succeeded";
  const generationFailed = tracked?.status === "failed";
  useEffect(() => {
    if (generationSucceeded && tracked && !invalidatedFor.current.has(tracked.id)) {
      invalidatedFor.current.add(tracked.id);
      invalidate();
    }
  }, [generationSucceeded, tracked, invalidate]);
  const shownNotice = generationSucceeded ? "Your new illustration is ready." : notice;
  const shownError = generationFailed && tracked
    ? `${avatarErrorMessage(tracked.error_code, tracked.error || "The illustration couldn't be made")} The previous avatar is unchanged.`
    : error;

  const fail = (err: unknown, fallback: string) => {
    if (err instanceof ApiError) {
      setError(avatarErrorMessage(err.code, `${err.detail || fallback}. Nothing changed — try again.`));
    } else {
      setError(`${fallback}. Nothing changed — check your connection and try again.`);
    }
  };

  const upload = useMutation({
    mutationFn: (picked: File) => {
      const form = new FormData();
      form.append("file", picked, picked.name);
      return apiUpload<AvatarOut>(base, form);
    },
    onSuccess: () => {
      setError(null);
      setNotice("Picture updated.");
      setFile(null);
      setMode("menu");
      invalidate();
    },
    onError: (err) => fail(err, "The upload failed"),
  });

  const remove = useMutation({
    mutationFn: () => api<AvatarOut>(base, { method: "DELETE" }),
    onSuccess: () => {
      setError(null);
      setNotice("Back to initials.");
      invalidate();
    },
    onError: (err) => fail(err, "Removing the picture failed"),
  });

  const generate = useMutation({
    mutationFn: () =>
      api<AvatarGenerationOut>(`${base}/generate`, { method: "POST", body: { prompt_hint: hint.trim() } }),
    onSuccess: (started) => {
      setError(null);
      setNotice(null);
      setTracking(started.id);
      void generation.refetch();
    },
    onError: (err) => fail(err, "Starting the illustration failed"),
  });

  const pick = (picked: File | null) => {
    setNotice(null);
    if (!picked) {
      setFile(null);
      return;
    }
    const problem = validateAvatarFile(picked);
    if (problem) {
      setError(problem);
      setFile(null);
      return;
    }
    setError(null);
    setFile(picked);
  };

  const busy = upload.isPending || remove.isPending || generate.isPending;
  const hasPicture = Boolean(agent.avatar_url);

  return (
    <Dialog title="Change avatar" description={`How ${agent.name} looks across Jhin.`} open onClose={onClose}>
      <div className="space-y-4">
        <div className="flex items-center gap-4">
          <Avatar name={agent.name} size="xl" src={agent.avatar_url} label={`${agent.name}'s current avatar`} />
          <div className="text-sm text-dim">
            <p className="font-medium text-ink">{agent.name}</p>
            <p>
              {agent.avatar_kind === "generated"
                ? "Using a generated illustration."
                : hasPicture
                  ? "Using an uploaded picture."
                  : "Using initials."}
            </p>
          </div>
        </div>

        {shownNotice ? (
          <p role="status" className="rounded-xl border border-ok/30 bg-ok-soft px-3.5 py-2.5 text-sm text-ok">
            {shownNotice}
          </p>
        ) : null}
        <ErrorNote message={shownError} />

        {running || (tracking && !tracked) ? (
          <div className="space-y-2 rounded-xl border border-accent/30 bg-accent-soft px-3.5 py-3">
            <Spinner label="Drawing the illustration… this usually takes under a minute." />
            {latest ? <DisclosureNote disclosure={latest.disclosure} /> : null}
            <p className="text-[13px] text-dim">You can close this dialog; the new picture appears when it’s ready.</p>
          </div>
        ) : null}

        {mode === "menu" ? (
          <div className="grid gap-2">
            <Button onClick={() => setMode("upload")} disabled={busy || running} className="justify-start">
              <Upload size={15} /> Upload a picture
            </Button>
            <Button onClick={() => setMode("generate")} disabled={busy || running} className="justify-start">
              <Sparkles size={15} /> Generate an illustration
            </Button>
            {hasPicture ? (
              <Button
                variant="danger"
                onClick={() => remove.mutate()}
                disabled={busy || running}
                className="justify-start"
              >
                <Trash2 size={15} /> Remove and use initials
              </Button>
            ) : null}
          </div>
        ) : null}

        {mode === "upload" ? (
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              if (file) upload.mutate(file);
            }}
          >
            <input
              ref={fileInput}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              className="sr-only"
              aria-label="Choose an image"
              onChange={(event) => pick(event.target.files?.[0] ?? null)}
            />
            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              className="flex w-full flex-col items-center gap-2 rounded-2xl border border-dashed border-line-strong bg-raised px-4 py-6 text-sm text-dim transition-colors hover:border-accent hover:text-ink"
            >
              {preview ? (
                // eslint-disable-next-line @next/next/no-img-element -- local object URL preview
                <img src={preview} alt="Preview of the chosen picture" className="h-20 w-20 rounded-xl object-cover" />
              ) : (
                <ImagePlus size={22} aria-hidden />
              )}
              <span>{file ? file.name : "Choose a PNG, JPEG, or WebP (up to 8 MB)"}</span>
              <span className="text-xs text-faint">It’s cropped to a square and resized; the original isn’t kept.</span>
            </button>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" type="button" onClick={() => setMode("menu")} disabled={upload.isPending}>
                Back
              </Button>
              <Button variant="primary" type="submit" disabled={!file || upload.isPending}>
                {upload.isPending ? "Uploading…" : "Use this picture"}
              </Button>
            </div>
          </form>
        ) : null}

        {mode === "generate" ? (
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              generate.mutate();
            }}
          >
            <p className="text-sm text-dim">
              Jhin asks an image model for a stylized portrait of {agent.name}. It’s deliberately illustrative and never
              a real likeness.
            </p>
            {latest && !running ? (
              <DisclosureNote disclosure={latest.disclosure} />
            ) : (
              <p className="rounded-xl border border-line bg-raised px-3.5 py-3 text-[13px] text-dim">
                The provider, model, and estimated cost are shown here as soon as the request starts. Only the agent’s
                public identity and your hint are sent.
              </p>
            )}
            <Field label="Hint (optional)" hint="Something like “warm colors, botanical motif”. Up to 300 characters.">
              <Input
                value={hint}
                maxLength={300}
                onChange={(event) => setHint(event.target.value)}
                placeholder="Style notes for the illustration"
              />
            </Field>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" type="button" onClick={() => setMode("menu")} disabled={generate.isPending}>
                Back
              </Button>
              <Button variant="primary" type="submit" disabled={generate.isPending || running}>
                {generate.isPending ? "Starting…" : "Generate"}
              </Button>
            </div>
            {latest?.status === "failed" && !tracked ? (
              <p className="text-[13px] text-dim">
                <StatusLabel tone="danger">Last attempt failed</StatusLabel>{" "}
                <span className="block">{avatarErrorMessage(latest.error_code, latest.error || "")}</span>
              </p>
            ) : null}
          </form>
        ) : null}
      </div>
    </Dialog>
  );
}
