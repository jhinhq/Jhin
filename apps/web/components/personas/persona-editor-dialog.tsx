"use client";

/** Write or edit a persona card. Every cap is counted as you type and the
 * block the agent will read re-renders on every keystroke; the content rules
 * (no tool names, links, override phrasing, or talk of permissions) are the
 * server's, and a 422 lands under the facet it names. */

import { useMutation } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";
import { useState } from "react";
import { PersonaTagBadges } from "@/components/personas/persona-badges";
import { PersonaBlockPreview } from "@/components/personas/persona-block-preview";
import { Button, Dialog, ErrorNote, Field, Input, Textarea } from "@/components/ui";
import { api } from "@/lib/api";
import {
  collapseWhitespace,
  draftFacets,
  draftFrom,
  FACET_SPECS,
  facetChars,
  fieldErrorsFrom,
  parseTags,
  PERSONA_CAPS,
  toCreateInput,
  toUpdateInput,
  validatePersonaForm,
  type PersonaDraft,
  type PersonaFieldKey,
  type PersonaFormErrors,
  type TextFacetKey,
} from "@/lib/personas";
import type { Persona } from "@/lib/types";

const NO_ERRORS: PersonaFormErrors = { fields: {}, general: null };

/** Rendered inside `Field` (a `<label>`), so a span, never a block element. */
function FieldError({ message }: { message: string | undefined }) {
  if (!message) return null;
  return (
    <span role="alert" className="block text-[13px] text-danger">
      {message}
    </span>
  );
}

function Count({
  value,
  limit,
  testId,
}: {
  value: number;
  limit: number;
  testId: string;
}) {
  return (
    <span
      data-testid={testId}
      className={`ml-auto tabular-nums ${value > limit ? "text-danger" : "text-faint"}`}
    >
      {value}/{limit}
    </span>
  );
}

export function PersonaEditorDialog({
  workspaceId,
  initial,
  onClose,
  onSaved,
}: {
  workspaceId: string;
  initial: Persona | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const creating = initial === null;
  const [draft, setDraft] = useState<PersonaDraft>(() => draftFrom(initial));
  const [serverErrors, setServerErrors] = useState<PersonaFormErrors>(NO_ERRORS);

  const clientErrors = validatePersonaForm(draft, creating);
  const errorFor = (key: PersonaFieldKey) => serverErrors.fields[key] ?? clientErrors[key];

  // Typing in a field clears what the server said about it — the message was
  // about text that is no longer there. The card total spans every facet, so
  // any facet edit clears that one too.
  const clearServerErrors = (...keys: PersonaFieldKey[]) =>
    setServerErrors((previous) => {
      if (!keys.some((key) => key in previous.fields)) return previous;
      const fields = { ...previous.fields };
      for (const key of keys) delete fields[key];
      return { ...previous, fields };
    });

  const setText = (key: "name" | "display_name" | "description", value: string) => {
    setDraft((previous) => ({ ...previous, [key]: value }));
    clearServerErrors(key);
  };
  const setTags = (value: string) => {
    setDraft((previous) => ({ ...previous, tagsInput: value }));
    clearServerErrors("tags");
  };
  const setFacet = (key: TextFacetKey, value: string) => {
    setDraft((previous) => ({ ...previous, facets: { ...previous.facets, [key]: value } }));
    clearServerErrors(key, "facets");
  };
  const setNever = (never: string[]) => {
    setDraft((previous) => ({ ...previous, facets: { ...previous.facets, never } }));
    clearServerErrors("never", "facets");
  };

  const save = useMutation({
    mutationFn: () =>
      initial === null
        ? api<Persona>(`/api/v1/workspaces/${workspaceId}/personas`, {
            method: "POST",
            body: toCreateInput(draft),
          })
        : api<Persona>(`/api/v1/workspaces/${workspaceId}/personas/${initial.id}`, {
            method: "PATCH",
            body: toUpdateInput(draft),
          }),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (error) => setServerErrors(fieldErrorsFrom(error, "Saving the persona failed.")),
  });

  const neverRows = draft.facets.never;
  const total = facetChars(draftFacets(draft));
  const canSave = Object.keys(clientErrors).length === 0 && !save.isPending;

  return (
    <Dialog
      wide
      open
      title={creating ? "New persona" : "Edit persona"}
      description="A persona shapes how an agent says things — never what it may do."
      onClose={onClose}
    >
      <div className="space-y-4">
        <Field
          label="Name"
          hint={
            creating
              ? "Lowercase letters, digits, and hyphens — this is how agents refer to it. It can’t change later."
              : "Can’t change after creation — agents refer to the persona by this name."
          }
        >
          <Input
            aria-label="Name"
            value={draft.name}
            disabled={!creating}
            maxLength={PERSONA_CAPS.name}
            placeholder="house-style"
            onChange={(event) => setText("name", event.target.value)}
          />
          <FieldError message={errorFor("name")} />
        </Field>

        <Field label="Display name">
          <Input
            aria-label="Display name"
            value={draft.display_name}
            maxLength={PERSONA_CAPS.displayName}
            placeholder="House Style"
            onChange={(event) => setText("display_name", event.target.value)}
          />
          <span className="mt-1 flex items-center justify-between text-xs">
            <FieldError message={errorFor("display_name")} />
            <Count
              value={collapseWhitespace(draft.display_name).length}
              limit={PERSONA_CAPS.displayName}
              testId="count-display_name"
            />
          </span>
        </Field>

        <Field label="Description" hint="One line for the gallery.">
          <Textarea
            aria-label="Description"
            rows={2}
            value={draft.description}
            maxLength={PERSONA_CAPS.description}
            placeholder="Calm flight-director cadence: status, go/no-go, next call."
            onChange={(event) => setText("description", event.target.value)}
          />
          <span className="mt-1 flex items-center justify-between text-xs">
            <FieldError message={errorFor("description")} />
            <Count
              value={collapseWhitespace(draft.description).length}
              limit={PERSONA_CAPS.description}
              testId="count-description"
            />
          </span>
        </Field>

        <div>
          <Field
            label="Tags"
            hint="Comma-separated, lowercase letters, digits, and hyphens. Add fun to mark a playful card."
          >
            <Input
              aria-label="Tags"
              value={draft.tagsInput}
              placeholder="professional, direct"
              onChange={(event) => setTags(event.target.value)}
            />
            <FieldError message={errorFor("tags")} />
          </Field>
          <div className="mt-1.5">
            <PersonaTagBadges tags={parseTags(draft.tagsInput)} />
          </div>
        </div>

        <div className="space-y-3 border-t border-line pt-4">
          <div>
            <h3 className="font-display text-sm font-semibold">How it works</h3>
            <p className="text-[13px] text-dim">
              Short and concrete. Say how the agent sounds, not what it may do — tool names, links,
              and anything about approvals or permissions are refused.
            </p>
          </div>
          {FACET_SPECS.map((spec) => (
            <Field key={spec.key} label={spec.label} hint={spec.hint}>
              <Textarea
                aria-label={spec.label}
                rows={2}
                value={draft.facets[spec.key]}
                maxLength={PERSONA_CAPS.facet}
                placeholder={spec.placeholder}
                onChange={(event) => setFacet(spec.key, event.target.value)}
              />
              <span className="mt-1 flex items-center justify-between text-xs">
                <FieldError message={errorFor(spec.key)} />
                <Count
                  value={collapseWhitespace(draft.facets[spec.key]).length}
                  limit={PERSONA_CAPS.facet}
                  testId={`count-${spec.key}`}
                />
              </span>
            </Field>
          ))}

          <fieldset className="space-y-2">
            <legend className="text-[13px] font-medium text-dim">Never</legend>
            <p className="text-[13px] text-faint">Up to six short, distinct things to avoid.</p>
            {neverRows.map((item, index) => (
              <div key={index} className="flex items-center gap-2">
                <Input
                  aria-label={`Never item ${index + 1}`}
                  value={item}
                  maxLength={PERSONA_CAPS.neverItem}
                  placeholder="Raise its voice, even in text"
                  onChange={(event) =>
                    setNever(neverRows.map((row, i) => (i === index ? event.target.value : row)))
                  }
                />
                <span className="flex shrink-0 items-center gap-1 text-xs">
                  <Count
                    value={collapseWhitespace(item).length}
                    limit={PERSONA_CAPS.neverItem}
                    testId={`count-never-${index}`}
                  />
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove never item ${index + 1}`}
                  onClick={() => setNever(neverRows.filter((_, i) => i !== index))}
                >
                  <X size={14} />
                </Button>
              </div>
            ))}
            <div className="flex items-center gap-3">
              <Button
                size="sm"
                disabled={neverRows.length >= PERSONA_CAPS.neverItems}
                onClick={() => setNever([...neverRows, ""])}
              >
                <Plus size={14} /> Add another
              </Button>
              <span className="text-xs text-faint">
                {neverRows.length} of {PERSONA_CAPS.neverItems}
              </span>
            </div>
            <FieldError message={errorFor("never")} />
          </fieldset>

          <div>
            <p
              data-testid="card-total"
              className={`text-xs tabular-nums ${total > PERSONA_CAPS.card ? "text-danger" : "text-faint"}`}
            >
              {total.toLocaleString()} / 1,500 characters across the facets
            </p>
            <FieldError message={errorFor("facets")} />
          </div>
        </div>

        <div className="border-t border-line pt-4">
          <h3 className="mb-1.5 font-display text-sm font-semibold">What the agent will read</h3>
          <PersonaBlockPreview
            name={draft.name || "persona"}
            displayName={draft.display_name}
            facets={draftFacets(draft)}
            audience="both"
          />
        </div>

        <ErrorNote message={serverErrors.general} />
        <div className="flex flex-col items-end gap-1.5">
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" disabled={!canSave} onClick={() => save.mutate()}>
              {save.isPending ? "Saving…" : creating ? "Create persona" : "Save changes"}
            </Button>
          </div>
          {!creating ? (
            <p className="text-xs text-faint">
              Changes reach every agent wearing it on their next run.
            </p>
          ) : null}
        </div>
      </div>
    </Dialog>
  );
}
