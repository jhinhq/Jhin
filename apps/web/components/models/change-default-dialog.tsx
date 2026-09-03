"use client";

/** Pick the workspace default model from radio-style cards — one per
 * profile, each saying in plain language what the model is good at and
 * roughly what it costs. Confirm PATCHes the workspace's
 * `default_model_profile_id`; nothing changes until then. */

import { useMutation } from "@tanstack/react-query";
import { type KeyboardEvent as ReactKeyboardEvent, useRef, useState } from "react";
import { PriceLine } from "@/components/models/price-line";
import { Badge, Button, Dialog, ErrorNote, focusRing, rovingIndex } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { capabilitySummary } from "@/lib/models";
import type { ModelProfile, ModelProvider } from "@/lib/types";

export function ChangeDefaultDialog({
  workspaceId,
  profiles,
  providers,
  currentDefaultId,
  onClose,
  onChanged,
}: {
  workspaceId: string;
  profiles: ModelProfile[];
  providers: ModelProvider[];
  currentDefaultId: string | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(currentDefaultId);
  const optionRefs = useRef<Map<string, HTMLButtonElement>>(new Map());

  // Roving tabindex: the chosen card is the group's single tab stop (the
  // first card when nothing is chosen yet), and arrow keys move focus and
  // selection together — same pattern as the Tabs primitive in ui.tsx.
  const tabStopId = profiles.some((p) => p.id === selectedId)
    ? selectedId
    : (profiles[0]?.id ?? null);

  const focusOption = (id: string) => {
    optionRefs.current.get(id)?.focus();
    setSelectedId(id);
  };

  const onOptionKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, id: string) => {
    const index = profiles.findIndex((profile) => profile.id === id);
    if (index === -1 || profiles.length === 0) return;
    const next = rovingIndex(event.key, index, profiles.length);
    if (next === null) return;
    event.preventDefault();
    focusOption(profiles[next].id);
  };

  const save = useMutation({
    mutationFn: () =>
      api(`/api/v1/workspaces/${workspaceId}`, {
        method: "PATCH",
        body: { default_model_profile_id: selectedId },
      }),
    onSuccess: () => {
      onChanged();
      onClose();
    },
  });
  const saveError =
    save.error instanceof ApiError ? save.error.detail : save.error ? "Setting the default failed." : null;

  return (
    <Dialog
      title="Change the default model"
      description="Agents use the workspace default unless given their own model."
      open
      onClose={onClose}
      wide
    >
      <div className="space-y-4">
        <div
          role="radiogroup"
          aria-label="Default model"
          className="grid gap-2 sm:grid-cols-2"
        >
          {profiles.map((profile) => {
            const provider = providers.find((p) => p.id === profile.provider_id);
            const selected = selectedId === profile.id;
            return (
              <button
                key={profile.id}
                ref={(node) => {
                  if (node) optionRefs.current.set(profile.id, node);
                  else optionRefs.current.delete(profile.id);
                }}
                type="button"
                role="radio"
                aria-checked={selected}
                tabIndex={profile.id === tabStopId ? 0 : -1}
                data-testid={`default-option-${profile.id}`}
                onClick={() => setSelectedId(profile.id)}
                onKeyDown={(event) => onOptionKeyDown(event, profile.id)}
                className={`flex flex-col gap-1 rounded-2xl border px-4 py-3 text-left transition-colors ${focusRing} ${
                  selected
                    ? "border-accent bg-accent-soft"
                    : "border-line bg-surface hover:border-line-strong"
                }`}
              >
                <span className="flex items-center gap-2">
                  <span className="truncate font-display text-sm font-semibold text-ink">
                    {profile.display_name}
                  </span>
                  {profile.id === currentDefaultId ? <Badge tone="info">Default</Badge> : null}
                </span>
                <span className="text-xs text-faint">{provider?.display_name ?? "—"}</span>
                {capabilitySummary(profile) ? (
                  <span className="text-[13px] text-dim">{capabilitySummary(profile)}</span>
                ) : null}
                {/* Same words as the model row and the hero: the API says an
                    assumed-free profile resolves to $0, so "no price set"
                    here would contradict them. */}
                <PriceLine profile={profile} variant="option" />
              </button>
            );
          })}
        </div>
        <ErrorNote message={saveError} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={selectedId === null || selectedId === currentDefaultId || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Make it the default"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
