"use client";

/** Models page (plan 15, 17.2 nav): the decisions first — the workspace
 * default model as the one shadowed hero, then every model as a row with
 * its price and (on an Ollama host) whether it is loaded, then each Ollama
 * host's own Local models block, then the providers as status rows — with
 * spend and the pricing machinery folded behind two disclosures at the
 * bottom. Every dialog lives in components/models/; this file is the layout
 * and the one switch that mounts a single dialog at a time. */

import { Plug, Plus } from "lucide-react";
import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { LoadError } from "@/components/company/bits";
import { AdminKeyDialog } from "@/components/models/admin-key-dialog";
import { ChangeDefaultDialog } from "@/components/models/change-default-dialog";
import { DefaultModelCard } from "@/components/models/default-model-card";
import { OllamaHostSection } from "@/components/models/ollama-panel";
import { PricingSection } from "@/components/models/pricing-section";
import { ProfileCard } from "@/components/models/profile-card";
import { ProfileDialog } from "@/components/models/profile-dialog";
import { ProviderCard } from "@/components/models/provider-card";
import { ProviderDialog } from "@/components/models/provider-dialog";
import { ProviderManageDialog } from "@/components/models/provider-manage-dialog";
import { BudgetBanner, SpendDisclosure } from "@/components/models/spend-disclosure";
import { Button, EmptyState, Spinner } from "@/components/ui";
import {
  useInvalidateModels,
  useModelProfiles,
  useModelProviders,
  usePricingStatus,
  useWorkspaceDetail,
  useWorkspaceSpend,
} from "@/lib/hooks";
import { providerTypeLabel, type ProfilePrefill } from "@/lib/models";
import { useOllamaHosts } from "@/lib/ollama-host";
import type { ProfilePricing } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

/** The one dialog open at a time, by id rather than row, so a verify or an
 * edit re-renders it with the freshly invalidated data instead of a
 * snapshot from when it opened. Manage → Edit and Manage → Add admin key
 * replace Manage and return to it, so no dialog ever mounts on another. */
type OpenDialog =
  | { kind: "provider-create" }
  | { kind: "provider-edit"; providerId: string; returnTo?: "manage" }
  | { kind: "provider-manage"; providerId: string }
  | { kind: "admin-key"; providerId: string; returnTo?: "manage" }
  | { kind: "profile-create"; prefill?: ProfilePrefill }
  | { kind: "profile-edit"; profileId: string }
  | { kind: "change-default" }
  | null;

const HEADING = "mb-3 font-display text-base font-semibold tracking-tight text-ink";
const LIST = "divide-y divide-line rounded-2xl border border-line bg-surface";

export default function ModelsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");

  // The providers listing is admin-only server-side; asking anyway would turn
  // every member's visit into a 403 reported as a load failure.
  const providers = useModelProviders(workspaceId, isAdmin);
  const profiles = useModelProfiles(workspaceId);
  const detail = useWorkspaceDetail(workspaceId);
  const spend = useWorkspaceSpend(workspaceId);
  const pricing = usePricingStatus(workspaceId);
  const invalidate = useInvalidateModels(workspaceId);
  // One live subscription per Ollama host, made here so everything that
  // describes the host — its Local models block, every model row on it, the
  // default hero — reads the same answer instead of each polling for its own.
  const hosts = useOllamaHosts(
    workspaceId,
    (providers.data ?? []).filter((p) => p.type === "ollama").map((p) => p.id),
  );

  const [dialog, setDialog] = useState<OpenDialog>(null);

  const pricingByProfile = new Map<string, ProfilePricing>(
    (pricing.data?.profiles ?? []).map((row) => [row.profile_id, row]),
  );

  // A disabled query stays pending forever, so the non-admin path must not
  // wait on providers.
  if ((isAdmin && providers.isPending) || profiles.isPending || detail.isPending) {
    return (
      <>
        <PageHeader title="Models" />
        <PageBody>
          <Spinner label="Loading model configuration…" />
        </PageBody>
      </>
    );
  }

  if ((isAdmin && providers.isError) || profiles.isError || detail.isError) {
    return (
      <>
        <PageHeader title="Models" />
        <PageBody>
          <LoadError
            what="your model setup"
            onRetry={() => {
              if (isAdmin) void providers.refetch();
              void profiles.refetch();
              void detail.refetch();
            }}
          />
        </PageBody>
      </>
    );
  }

  const providerList = providers.data ?? [];
  const profileList = profiles.data ?? [];
  const defaultProfileId = detail.data?.default_model_profile_id ?? null;
  const defaultProfile = profileList.find((profile) => profile.id === defaultProfileId) ?? null;
  const defaultProvider = defaultProfile
    ? (providerList.find((provider) => provider.id === defaultProfile.provider_id) ?? null)
    : null;
  // The local host is the provider people come to check, so it leads the
  // list. The sort is stable, so the API's order holds within each group.
  const orderedProviders = [...providerList].sort(
    (a, b) => Number(b.type === "ollama") - Number(a.type === "ollama"),
  );
  const ollamaHosts = providerList.flatMap((provider) => {
    const host = hosts.get(provider.id);
    return host ? [{ provider, host }] : [];
  });
  const profilesFor = (providerId: string) =>
    profileList.filter((profile) => profile.provider_id === providerId);
  const spendFor = (providerId: string) =>
    spend.data?.providers.find((row) => row.provider_id === providerId)?.spent_month_micros;
  // A fresh workspace gets one empty state with the next action, never
  // three stacked boxes.
  const noProviders = isAdmin && providerList.length === 0;
  const closeDialog = () => setDialog(null);
  const providerById = (id: string) => providerList.find((p) => p.id === id) ?? null;

  const renderDialog = () => {
    switch (dialog?.kind) {
      case "provider-create":
        return (
          <ProviderDialog workspaceId={workspaceId} onClose={closeDialog} onCreated={invalidate} />
        );
      case "provider-edit": {
        const provider = providerById(dialog.providerId);
        if (!provider) return null;
        const back =
          dialog.returnTo === "manage"
            ? () => setDialog({ kind: "provider-manage", providerId: provider.id })
            : closeDialog;
        return (
          <ProviderDialog
            workspaceId={workspaceId}
            existing={provider}
            onClose={back}
            onCreated={invalidate}
          />
        );
      }
      case "provider-manage": {
        const provider = providerById(dialog.providerId);
        if (!provider) return null;
        return (
          <ProviderManageDialog
            workspaceId={workspaceId}
            provider={provider}
            typeLabel={providerTypeLabel(provider.type)}
            profileCount={profilesFor(provider.id).length}
            isDefaultProvider={profilesFor(provider.id).some((p) => p.id === defaultProfileId)}
            isAdmin={isAdmin}
            onClose={closeDialog}
            onChanged={invalidate}
            onEdit={() =>
              setDialog({ kind: "provider-edit", providerId: provider.id, returnTo: "manage" })
            }
            onAddAdminKey={() =>
              setDialog({ kind: "admin-key", providerId: provider.id, returnTo: "manage" })
            }
          />
        );
      }
      case "admin-key": {
        const provider = providerById(dialog.providerId);
        if (!provider) return null;
        const back =
          dialog.returnTo === "manage"
            ? () => setDialog({ kind: "provider-manage", providerId: provider.id })
            : closeDialog;
        return (
          <AdminKeyDialog
            workspaceId={workspaceId}
            provider={provider}
            onClose={back}
            onSaved={() => {
              invalidate();
              back();
            }}
          />
        );
      }
      case "profile-create":
        return (
          <ProfileDialog
            workspaceId={workspaceId}
            providers={providerList}
            prefill={dialog.prefill}
            onClose={closeDialog}
            onCreated={invalidate}
          />
        );
      case "profile-edit": {
        const profile = profileList.find((p) => p.id === dialog.profileId);
        if (!profile) return null;
        return (
          <ProfileDialog
            workspaceId={workspaceId}
            providers={providerList}
            existing={profile}
            isDefault={profile.id === defaultProfileId}
            pricing={pricingByProfile.get(profile.id)}
            onClose={closeDialog}
            onCreated={invalidate}
          />
        );
      }
      case "change-default":
        return (
          <ChangeDefaultDialog
            workspaceId={workspaceId}
            profiles={profileList}
            providers={providerList}
            currentDefaultId={defaultProfileId}
            onClose={closeDialog}
            onChanged={invalidate}
          />
        );
      default:
        return null;
    }
  };

  return (
    <>
      <PageHeader
        title="Models"
        description="Choose the AI models your agents think with."
        actions={
          isAdmin ? (
            <>
              <Button size="sm" onClick={() => setDialog({ kind: "provider-create" })}>
                <Plug size={14} /> Add provider
              </Button>
              <Button
                size="sm"
                variant="primary"
                disabled={providerList.length === 0}
                title={providerList.length === 0 ? "Connect a provider first" : undefined}
                onClick={() => setDialog({ kind: "profile-create" })}
              >
                <Plus size={14} /> New profile
              </Button>
            </>
          ) : null
        }
      />
      <PageBody className="space-y-8">
        {spend.data ? <BudgetBanner spend={spend.data} /> : null}

        {!noProviders && profileList.length > 0 ? (
          <section>
            <h2 className={HEADING}>Default model</h2>
            <DefaultModelCard
              profile={defaultProfile}
              provider={defaultProvider}
              isAdmin={isAdmin}
              host={defaultProfile ? hosts.get(defaultProfile.provider_id) : undefined}
              onChange={() => setDialog({ kind: "change-default" })}
            />
          </section>
        ) : null}

        {!noProviders ? (
          <section>
            <h2 className={HEADING}>Models</h2>
            {profileList.length === 0 ? (
              <EmptyState
                title="No model profiles yet"
                description="A profile is a named model on a provider with pricing — agents reference profiles, never raw providers."
                action={
                  isAdmin ? (
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => setDialog({ kind: "profile-create" })}
                    >
                      <Plus size={14} /> New profile
                    </Button>
                  ) : undefined
                }
              />
            ) : (
              <ul className={LIST}>
                {profileList.map((profile) => (
                  <ProfileCard
                    key={profile.id}
                    profile={profile}
                    provider={providerList.find((p) => p.id === profile.provider_id)}
                    isDefault={profile.id === defaultProfileId}
                    isAdmin={isAdmin}
                    workspaceId={workspaceId}
                    pricing={pricingByProfile.get(profile.id)}
                    pricingPages={pricing.data?.pricing_pages}
                    host={hosts.get(profile.provider_id)}
                    onChanged={invalidate}
                    onEdit={(row) => setDialog({ kind: "profile-edit", profileId: row.id })}
                  />
                ))}
              </ul>
            )}
          </section>
        ) : null}

        {ollamaHosts.length > 0 ? (
          <section>
            <h2 className="mb-1 font-display text-base font-semibold tracking-tight text-ink">
              Local models
            </h2>
            <p className="mb-3 text-sm text-dim">
              What each Ollama host has in memory. Load a model before a run so its first reply
              doesn&apos;t wait on the weights.
            </p>
            <div className="space-y-4">
              {ollamaHosts.map(({ provider, host }) => (
                <OllamaHostSection
                  key={provider.id}
                  provider={provider}
                  host={host}
                  isAdmin={isAdmin}
                  profiles={profileList}
                  onUseAsModel={(prefill) => setDialog({ kind: "profile-create", prefill })}
                />
              ))}
            </div>
          </section>
        ) : null}

        <section>
          <h2 className={HEADING}>Providers</h2>
          {!isAdmin ? (
            <p className="text-sm text-dim">
              Provider accounts and API keys are managed by workspace admins.
            </p>
          ) : providerList.length === 0 ? (
            <EmptyState
              title="No model providers yet"
              description="Connect OpenAI, Anthropic, OpenRouter, Ollama, or any OpenAI-compatible endpoint. API keys are envelope-encrypted at rest and never shown again."
              action={
                <Button variant="primary" onClick={() => setDialog({ kind: "provider-create" })}>
                  <Plus size={14} /> Add first provider
                </Button>
              }
            />
          ) : (
            <ul className={LIST}>
              {orderedProviders.map((provider) => (
                <ProviderCard
                  key={provider.id}
                  provider={provider}
                  typeLabel={providerTypeLabel(provider.type)}
                  profileCount={profilesFor(provider.id).length}
                  spentMonthMicros={spendFor(provider.id)}
                  onManage={() => setDialog({ kind: "provider-manage", providerId: provider.id })}
                />
              ))}
            </ul>
          )}
        </section>

        <div className="space-y-3">
          {spend.data ? <SpendDisclosure spend={spend.data} /> : null}
          <PricingSection
            workspaceId={workspaceId}
            isAdmin={isAdmin}
            status={pricing.data}
            isPending={pricing.isPending}
            onChanged={invalidate}
          />
        </div>
      </PageBody>

      {renderDialog()}
    </>
  );
}
