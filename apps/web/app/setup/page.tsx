"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { AuthCard } from "@/components/auth-card";
import { Button, ErrorNote, Field, Input } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useBootstrapStatus } from "@/lib/hooks";
import type { MeResponse } from "@/lib/types";

/** First-run owner bootstrap (plan 43 steps 1-2). Disables itself once an
 * owner exists — the API enforces this server-side. */
export default function SetupPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const bootstrap = useBootstrapStatus();
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");

  useEffect(() => {
    if (bootstrap.data && !bootstrap.data.needs_bootstrap) router.replace("/login");
  }, [bootstrap.data, router]);

  const submit = useMutation({
    mutationFn: () =>
      api<MeResponse>("/api/v1/auth/bootstrap", {
        method: "POST",
        body: {
          display_name: displayName,
          email,
          password,
          workspace_name: workspaceName,
        },
      }),
    onSuccess: (me) => {
      queryClient.setQueryData(["me"], me);
      router.replace("/");
    },
  });

  const errorMessage =
    submit.error instanceof ApiError
      ? submit.error.detail
      : submit.error
        ? "Setup failed — is the API reachable?"
        : null;

  return (
    <AuthCard
      title="Welcome to Jhin"
      subtitle="Let's set up your account and name your workspace. Takes about a minute."
    >
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          submit.mutate();
        }}
      >
        <Field label="Your name">
          <Input
            required
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder="Ada Lovelace"
            maxLength={200}
          />
        </Field>
        <Field label="Email">
          <Input
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@company.com"
          />
        </Field>
        <Field label="Password" hint="At least 10 characters.">
          <Input
            type="password"
            autoComplete="new-password"
            required
            minLength={10}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
        <Field label="Workspace name">
          <Input
            required
            value={workspaceName}
            onChange={(event) => setWorkspaceName(event.target.value)}
            placeholder="Acme Inc"
            maxLength={200}
          />
        </Field>
        <ErrorNote message={errorMessage} />
        <Button type="submit" variant="primary" className="w-full" disabled={submit.isPending}>
          {submit.isPending ? "Creating…" : "Create owner & workspace"}
        </Button>
      </form>
    </AuthCard>
  );
}
