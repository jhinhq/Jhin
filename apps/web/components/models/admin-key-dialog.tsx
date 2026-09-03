"use client";

/** Attach an OpenAI admin key to a provider after the fact, so the Balance
 * block can read month-to-date spend. The key goes into the encrypted secret
 * store like any other and the provider row only references it. */

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Dialog, ErrorNote, Field, Input } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { storeApiKey } from "@/lib/model-secrets";
import type { ModelProvider } from "@/lib/types";

export const ADMIN_KEY_HINT =
  "Optional. OpenAI has no balance API; an admin key lets Jhin read month-to-date spend. Create one in the OpenAI dashboard → Settings → Organization → Admin keys. Stored encrypted, never displayed.";

export function AdminKeyDialog({
  workspaceId,
  provider,
  onClose,
  onSaved,
}: {
  workspaceId: string;
  provider: ModelProvider;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [adminKey, setAdminKey] = useState("");
  const save = useMutation({
    mutationFn: async () => {
      const secretId = await storeApiKey(
        workspaceId,
        `${provider.display_name} admin key`,
        adminKey.trim(),
      );
      await api<ModelProvider>(`/api/v1/workspaces/${workspaceId}/model-providers/${provider.id}`, {
        method: "PATCH",
        body: { admin_secret_id: secretId },
      });
    },
    onSuccess: onSaved,
  });
  return (
    <Dialog title="Add OpenAI admin key" open onClose={onClose}>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <Field label="Admin key" hint={ADMIN_KEY_HINT}>
          <Input
            type="password"
            autoComplete="off"
            required
            value={adminKey}
            onChange={(e) => setAdminKey(e.target.value)}
            placeholder="sk-admin-…"
          />
        </Field>
        <ErrorNote
          message={save.error ? errorText(save.error, "Saving the admin key failed.") : null}
        />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={save.isPending || !adminKey.trim()}>
            {save.isPending ? "Saving…" : "Save admin key"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
