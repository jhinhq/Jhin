"use client";

/** Everything operational about one provider, behind its Manage button:
 * endpoint and credential facts, the live verify check, balance and loaded
 * credits, and the edit/delete actions. This is the old provider card's
 * content, relocated — it stopped being the first thing everyone had to
 * read. The one thing that went the other way is an Ollama host's local
 * models: they are live state people come to check and act on, so they have
 * their own Local models block on the page, and this dialog only points
 * there. */

import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, HardDrive, KeyRound, ShieldCheck, Wallet, XCircle } from "lucide-react";
import { useState } from "react";
import { Button, ConfirmDialog, Dialog, ErrorNote } from "@/components/ui";
import { ProviderStatus } from "@/components/models/provider-card";
import { api, errorText } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useProviderBalance } from "@/lib/hooks";
import {
  balanceSourceLabel,
  dollarInputToMicros,
  formatMicrosAsDollars,
  microsToDollarInput,
} from "@/lib/models";
import type { ModelProvider } from "@/lib/types";

export function ProviderManageDialog({
  workspaceId,
  provider,
  typeLabel,
  profileCount,
  isDefaultProvider,
  isAdmin,
  onClose,
  onChanged,
  onEdit,
  onAddAdminKey,
}: {
  workspaceId: string;
  provider: ModelProvider;
  /** Human name of the provider type ("OpenAI", "Ollama (local)", …). */
  typeLabel: string;
  profileCount: number;
  /** Whether the workspace default profile runs on this provider. */
  isDefaultProvider: boolean;
  isAdmin: boolean;
  onClose: () => void;
  onChanged: () => void;
  /** Opens the provider edit dialog (kept on the page beside the create one). */
  onEdit: () => void;
  /** Opens the OpenAI admin-key dialog (kept on the page). */
  onAddAdminKey: () => void;
}) {
  const [verifyResult, setVerifyResult] = useState<{ ok: boolean; detail: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const verify = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; detail: string }>(
        `/api/v1/workspaces/${workspaceId}/model-providers/${provider.id}/verify`,
        { method: "POST" },
      ),
    onSuccess: (result) => {
      setActionError(null);
      setVerifyResult(result);
      onChanged();
    },
    onError: (error) => setActionError(errorText(error, "Verification failed.")),
  });

  const remove = useMutation({
    mutationFn: () =>
      api<void>(`/api/v1/workspaces/${workspaceId}/model-providers/${provider.id}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      setActionError(null);
      onChanged();
      onClose();
    },
    onError: (error) => {
      setConfirmDelete(false);
      setActionError(errorText(error, "Deleting the provider failed."));
    },
  });

  const deleteConsequence =
    profileCount > 0
      ? `This also removes its ${
          profileCount === 1 ? "model profile" : `${profileCount} model profiles`
        }${isDefaultProvider ? " and clears the workspace default" : ""}.`
      : "This cannot be undone.";

  return (
    <Dialog title={provider.display_name} description={typeLabel} open onClose={onClose} wide>
      <div className="space-y-4">
        <ProviderStatus provider={provider} />

        <dl className="space-y-1 text-sm text-dim">
          {provider.base_url ? (
            <div className="truncate">
              <dt className="inline text-faint">Endpoint: </dt>
              <dd className="inline font-mono text-xs">{provider.base_url}</dd>
            </div>
          ) : null}
          <div>
            <dt className="inline text-faint">Credential: </dt>
            <dd className="inline">
              {provider.secret_id ? (
                <span className="inline-flex items-center gap-1">
                  <KeyRound size={12} /> encrypted secret
                </span>
              ) : (
                "none"
              )}
            </dd>
          </div>
          <div>
            <dt className="inline text-faint">Profiles: </dt>
            <dd className="inline">{profileCount}</dd>
          </div>
          <div>
            <dt className="inline text-faint">Last verified: </dt>
            <dd className="inline">
              {provider.last_verified_at ? formatDateTime(provider.last_verified_at) : "never"}
            </dd>
          </div>
        </dl>

        {verifyResult ? (
          <p
            className={`flex items-start gap-1.5 rounded-xl px-3 py-2 text-sm ${
              verifyResult.ok ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
            }`}
          >
            {verifyResult.ok ? (
              <CheckCircle2 size={14} className="mt-0.5 shrink-0" />
            ) : (
              <XCircle size={14} className="mt-0.5 shrink-0" />
            )}
            <span className="min-w-0 break-words">{verifyResult.detail}</span>
          </p>
        ) : provider.last_error ? (
          <p className="rounded-xl bg-danger-soft px-3 py-2 text-sm text-danger">
            {provider.last_error}
          </p>
        ) : null}

        <ErrorNote message={actionError} />

        {/* A local host has no balance to show; what it has is models, and
            those live in the Local models block on the page — one place, not
            two, so a load started there is never missing here. */}
        {provider.type === "ollama" ? (
          <p
            data-testid="ollama-manage-note"
            className="flex items-start gap-1.5 rounded-xl bg-raised px-3 py-2 text-xs text-faint"
          >
            <HardDrive size={12} aria-hidden className="mt-0.5 shrink-0" />
            <span>
              Installed and loaded models, with Load and Unload, are under Local models on the
              Models page.
            </span>
          </p>
        ) : (
          <BalanceBlock
            provider={provider}
            isAdmin={isAdmin}
            workspaceId={workspaceId}
            onChanged={onChanged}
            onError={setActionError}
            onAddAdminKey={onAddAdminKey}
          />
        )}

        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
          <Button size="sm" onClick={() => verify.mutate()} disabled={verify.isPending}>
            <ShieldCheck size={13} /> {verify.isPending ? "Verifying…" : "Verify"}
          </Button>
          {isAdmin ? (
            <>
              <Button size="sm" onClick={onEdit}>
                Edit
              </Button>
              <Button
                size="sm"
                variant="danger"
                className="ml-auto"
                disabled={remove.isPending}
                onClick={() => setConfirmDelete(true)}
              >
                Delete
              </Button>
            </>
          ) : null}
        </div>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        title={`Delete provider “${provider.display_name}”?`}
        body={deleteConsequence}
        confirmLabel="Delete provider"
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
        onClose={() => setConfirmDelete(false)}
      />
    </Dialog>
  );
}

/** Balance and spend for one provider: live remaining when the provider
 * reports it, otherwise Jhin's tracked spend with an optional "loaded
 * credits" figure so an estimated remaining amount can be shown. */
export function BalanceBlock({
  provider,
  isAdmin,
  workspaceId,
  onChanged,
  onError,
  onAddAdminKey,
}: {
  provider: ModelProvider;
  isAdmin: boolean;
  workspaceId: string;
  onChanged: () => void;
  onError: (message: string | null) => void;
  onAddAdminKey: () => void;
}) {
  const balance = useProviderBalance(workspaceId, provider.id);
  const [credits, setCredits] = useState<string | null>(null);

  const saveCredits = useMutation({
    mutationFn: (micros: number | null) =>
      api<ModelProvider>(`/api/v1/workspaces/${workspaceId}/model-providers/${provider.id}`, {
        method: "PATCH",
        body: { credits_loaded_micros: micros },
      }),
    onSuccess: () => {
      onError(null);
      setCredits(null);
      onChanged();
      void balance.refetch();
    },
    onError: (error) => onError(errorText(error, "Saving the loaded credits failed.")),
  });

  if (balance.isPending) {
    return (
      <div data-testid="balance-block" className="rounded-xl bg-raised px-3 py-2 text-xs text-faint">
        Loading balance…
      </div>
    );
  }
  if (balance.isError) {
    return (
      <div data-testid="balance-block" className="rounded-xl bg-raised px-3 py-2 text-xs text-faint">
        We couldn’t load the balance for this provider. Check your connection and try again.{" "}
        <button
          type="button"
          onClick={() => void balance.refetch()}
          className="font-medium text-ink underline"
        >
          Retry
        </button>
      </div>
    );
  }
  const data = balance.data;
  if (!data) {
    return (
      <div data-testid="balance-block" className="rounded-xl bg-raised px-3 py-2 text-xs text-faint">
        Balance unavailable.
      </div>
    );
  }

  const live = data.provider_remaining_micros !== null;
  const creditsValue = credits ?? microsToDollarInput(data.credits_loaded_micros);
  const showAddAdminKey = provider.type === "openai" && !provider.has_admin_key && isAdmin;

  return (
    <div data-testid="balance-block" className="space-y-1.5 rounded-xl bg-raised px-3 py-2 text-xs">
      <p className="flex items-center gap-1.5 font-medium text-ink">
        <Wallet size={12} aria-hidden /> Balance
      </p>
      {live ? (
        <p className="text-sm tabular-nums text-ink">
          {formatMicrosAsDollars(data.provider_remaining_micros)}{" "}
          <span className="text-xs text-dim">remaining</span>
        </p>
      ) : (
        <p className="text-dim">
          {data.source === "openai_admin" && data.provider_spent_month_micros !== null ? (
            <>
              Spent this month:{" "}
              <span className="tabular-nums text-ink">
                {formatMicrosAsDollars(data.provider_spent_month_micros)}
              </span>
            </>
          ) : (
            <>
              Spent this month through Jhin:{" "}
              <span className="tabular-nums text-ink">
                {formatMicrosAsDollars(data.tracked_spent_month_micros)}
              </span>
            </>
          )}
        </p>
      )}
      {!live ? (
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1 text-faint">
            Loaded credits $
            <input
              type="number"
              min="0"
              step="any"
              aria-label="Loaded credits in dollars"
              className="h-10 w-28 rounded-md border border-line bg-surface px-1.5 text-base tabular-nums text-ink md:text-xs"
              value={creditsValue}
              disabled={!isAdmin || saveCredits.isPending}
              onChange={(e) => setCredits(e.target.value)}
              onBlur={() => {
                if (credits === null) return;
                const micros = dollarInputToMicros(credits);
                if (micros !== (data.credits_loaded_micros ?? null)) saveCredits.mutate(micros);
                else setCredits(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  (e.target as HTMLInputElement).blur();
                }
              }}
            />
          </label>
          {data.estimated_remaining_micros !== null ? (
            <span className="tabular-nums text-ink">
              ≈ {formatMicrosAsDollars(data.estimated_remaining_micros)} remaining
            </span>
          ) : null}
        </div>
      ) : null}
      <p className="text-faint" title={data.detail ?? undefined}>
        {balanceSourceLabel(data.source)}
        {data.source === "tracked" && data.detail && data.detail !== "Tracked by Jhin"
          ? ` — ${data.detail}`
          : ""}
      </p>
      {showAddAdminKey ? (
        <Button size="sm" variant="ghost" onClick={onAddAdminKey}>
          <KeyRound size={12} /> Add admin key
        </Button>
      ) : null}
    </div>
  );
}
