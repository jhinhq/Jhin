"use client";

/** Admin "Change avatar" dialog with four tabs: pick a free brand-cube
 * Shape (default), Generate an illustration (paid, needs an image-capable
 * provider), Upload a picture, or Reset to initials. Failures keep the
 * previous avatar; the copy says so. */

import { useMutation } from "@tanstack/react-query";
import { ImagePlus, Sparkles, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Avatar } from "@/components/avatar";
import { ShapeAvatar } from "@/components/shape-avatar";
import { Button, Dialog, ErrorNote, Field, Input, Spinner, StatusLabel, Tabs, focusRing } from "@/components/ui";
import { api, ApiError, apiUpload } from "@/lib/api";
import { useAvatarGeneration, useInvalidateAvatar } from "@/lib/hooks";
import { avatarErrorMessage, formatDisclosureCost, identityAvatarProps, validateAvatarFile } from "@/lib/media";
import { AVATAR_PALETTE, AVATAR_SHAPES, defaultShapeFor } from "@/lib/shapes";
import type { Agent, AvatarGenerationOut, AvatarOut, ProviderDisclosure } from "@/lib/types";

type Tab = "shape" | "generate" | "upload" | "reset";

const TABS: { id: Tab; label: string }[] = [
  { id: "shape", label: "Shape" },
  { id: "generate", label: "Generate" },
  { id: "upload", label: "Upload" },
  { id: "reset", label: "Reset" },
];

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
  const derived = defaultShapeFor(agent.name);
  const [tab, setTab] = useState<Tab>("shape");
  const [selShape, setSelShape] = useState(agent.avatar_shape || derived.shape);
  const [selColor, setSelColor] = useState(agent.avatar_color || derived.color);
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

  const setShape = useMutation({
    mutationFn: () =>
      api<AvatarOut>(`${base}/shape`, { method: "PUT", body: { shape: selShape, color: selColor } }),
    onSuccess: () => {
      setError(null);
      setNotice("Shape saved. It shows everywhere right away — and it’s free.");
      invalidate();
    },
    onError: (err) => fail(err, "Saving the shape failed"),
  });

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
    onError: (err) => fail(err, "Removing the avatar failed"),
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

  const busy = setShape.isPending || upload.isPending || remove.isPending || generate.isPending;
  const hasPicture = Boolean(agent.avatar_url);
  const hasShape = Boolean(agent.avatar_shape && agent.avatar_color);
  const current =
    agent.avatar_kind === "generated"
      ? "Using a generated illustration."
      : agent.avatar_kind === "shape" || (!hasPicture && hasShape)
        ? "Using a free shape avatar."
        : hasPicture
          ? "Using an uploaded picture."
          : "Using initials.";

  return (
    <Dialog title="Change avatar" description={`How ${agent.name} looks across Jhin.`} open onClose={onClose} wide>
      <div className="space-y-4">
        <div className="flex items-center gap-4">
          <Avatar name={agent.name} size="xl" {...identityAvatarProps(agent)} label={`${agent.name}'s current avatar`} />
          <div className="text-sm text-dim">
            <p className="font-medium text-ink">{agent.name}</p>
            <p>{current}</p>
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

        <Tabs tabs={TABS} value={tab} onChange={(id) => setTab(id as Tab)} label="Avatar options" panelId="avatar-tab-panel" />

        <div id="avatar-tab-panel">
          {tab === "shape" ? (
            <div className="space-y-4">
              <p className="text-sm text-dim">
                A stack of the Jhin cubes in a color you pick. Free, instant, and drawn right in the app — nothing is
                sent anywhere.
              </p>
              <div className="flex flex-wrap items-start gap-5">
                <Avatar name={agent.name} size="xl" shape={selShape} color={selColor} label="Preview of the shape avatar" />
                <div className="min-w-0 flex-1 space-y-3">
                  <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Shape">
                    {AVATAR_SHAPES.map((spec) => {
                      const active = selShape === spec.id;
                      return (
                        <button
                          key={spec.id}
                          type="button"
                          role="radio"
                          aria-checked={active}
                          aria-label={spec.label}
                          title={spec.label}
                          onClick={() => setSelShape(spec.id)}
                          className={`flex h-12 w-12 items-center justify-center rounded-xl border transition-colors ${focusRing} ${
                            active ? "border-accent bg-accent-soft ring-2 ring-accent/40" : "border-line bg-raised hover:border-line-strong"
                          }`}
                        >
                          <ShapeAvatar shape={spec.id} color={selColor} className="h-7 w-7" />
                        </button>
                      );
                    })}
                  </div>
                  <div className="grid w-fit grid-cols-6 gap-1.5" role="radiogroup" aria-label="Color">
                    {AVATAR_PALETTE.map((color) => {
                      const active = selColor === color.hex;
                      return (
                        <button
                          key={color.hex}
                          type="button"
                          role="radio"
                          aria-checked={active}
                          aria-label={color.label}
                          title={color.label}
                          onClick={() => setSelColor(color.hex)}
                          className={`h-8 w-8 rounded-full border transition-transform ${focusRing} ${
                            active ? "scale-110 border-ink ring-2 ring-accent/40" : "border-line"
                          }`}
                          style={{ backgroundColor: color.hex }}
                        />
                      );
                    })}
                  </div>
                </div>
              </div>
              <div className="flex justify-end">
                <Button variant="primary" onClick={() => setShape.mutate()} disabled={busy}>
                  {setShape.isPending ? "Saving…" : "Use this shape"}
                </Button>
              </div>
            </div>
          ) : null}

          {tab === "generate" ? (
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                generate.mutate();
              }}
            >
              <p className="text-sm text-dim">
                <Sparkles size={14} className="mr-1 inline text-accent-strong" aria-hidden />
                Jhin asks an image model for a stylized portrait of {agent.name}. It’s deliberately illustrative and
                never a real likeness. <span className="font-medium text-ink">This costs money</span> and needs an
                image-capable provider — shapes and uploads are free.
              </p>
              {latest && !running ? (
                <DisclosureNote disclosure={latest.disclosure} />
              ) : (
                <p className="rounded-xl border border-line bg-raised px-3.5 py-3 text-[13px] text-dim">
                  The provider, model, and estimated cost are shown here as soon as the request starts. Only the
                  agent’s public identity and your hint are sent.
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
              <div className="flex justify-end">
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

          {tab === "upload" ? (
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
              <div className="flex justify-end">
                <Button variant="primary" type="submit" disabled={!file || upload.isPending}>
                  {upload.isPending ? "Uploading…" : "Use this picture"}
                </Button>
              </div>
            </form>
          ) : null}

          {tab === "reset" ? (
            <div className="space-y-3">
              <p className="text-sm text-dim">
                Go back to plain initials. Any uploaded picture, generated illustration, or shape avatar is removed.
              </p>
              <div className="flex justify-end">
                <Button
                  variant="danger"
                  onClick={() => remove.mutate()}
                  disabled={busy || running || (!hasPicture && !hasShape)}
                >
                  <Trash2 size={15} /> {remove.isPending ? "Removing…" : "Remove and use initials"}
                </Button>
              </div>
              {!hasPicture && !hasShape ? (
                <p className="text-xs text-faint">{agent.name} is already using initials.</p>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </Dialog>
  );
}
