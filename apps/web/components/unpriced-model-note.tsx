"use client";

/**
 * "We don't know this model's price" — and the two ways out of it.
 *
 * A profile with no price does not fail loudly; it quietly records every run
 * as costing $0, which makes the spend total and the budget wrong in the same
 * silent direction. So wherever an unpriced model appears we say so plainly,
 * link the provider's real pricing page, and put the two fields that fix it
 * right there rather than behind a dialog.
 */

import { ExternalLink } from "lucide-react";
import { useState } from "react";
import { Button, Field, Input } from "@/components/ui";
import { dollarInputToMicros, pricingPageUrl, PROVIDER_LABELS } from "@/lib/models";
import type { ModelProviderType } from "@/lib/types";

export interface UnpricedModelNoteProps {
  modelName: string;
  providerType: ModelProviderType;
  /** Server-supplied pricing page map; falls back to the built-in one. */
  pricingPages?: Record<string, string> | null;
  /** Runs already recorded at $0 because of the missing price. */
  runs?: number;
  /** Omitted for viewers: the fields only appear for admins who can save. */
  onSave?: (input: number | null, output: number | null) => void;
  saving?: boolean;
}

export function UnpricedModelNote({
  modelName,
  providerType,
  pricingPages,
  runs = 0,
  onSave,
  saving = false,
}: UnpricedModelNoteProps) {
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  const url = pricingPageUrl(providerType, pricingPages);
  const label = PROVIDER_LABELS[providerType] ?? providerType;
  const canSave = Boolean(input.trim() || output.trim());

  return (
    <div
      role="note"
      data-testid="unpriced-model-note"
      className="rounded-md border border-warn/40 bg-warn-soft px-3 py-2 text-xs text-warn"
    >
      <p className="font-medium">
        We don&apos;t know <code className="font-mono">{modelName}</code>&apos;s price, so spend
        for it won&apos;t be tracked.
      </p>
      {runs > 0 ? (
        <p className="mt-1 text-dim">
          {runs} {runs === 1 ? "run has" : "runs have"} already been recorded as $0.00.
        </p>
      ) : null}
      {url ? (
        <p className="mt-1">
          <a
            href={url}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex items-center gap-1 underline underline-offset-2"
          >
            {label}&apos;s pricing page
            <ExternalLink aria-hidden className="h-3 w-3" />
          </a>
        </p>
      ) : null}
      {onSave ? (
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <Field label="Input $ / 1M">
            <Input
              type="number"
              min="0"
              step="0.000001"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="2.00"
              aria-label={`Input dollars per million tokens for ${modelName}`}
              className="w-28"
            />
          </Field>
          <Field label="Output $ / 1M">
            <Input
              type="number"
              min="0"
              step="0.000001"
              value={output}
              onChange={(event) => setOutput(event.target.value)}
              placeholder="12.00"
              aria-label={`Output dollars per million tokens for ${modelName}`}
              className="w-28"
            />
          </Field>
          <Button
            size="sm"
            variant="primary"
            disabled={!canSave || saving}
            onClick={() => onSave(dollarInputToMicros(input), dollarInputToMicros(output))}
          >
            {saving ? "Saving…" : "Save prices"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
