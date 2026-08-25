"use client";

/** Accepting a workspace invitation: the invitee chooses their own password
 * and lands signed in (docs/architecture/rbac.md). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { ROLE_COPY } from "@/components/access";
import { AuthCard } from "@/components/auth-card";
import { Button, ErrorNote, Field, Input, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import type { InvitationPreview, MeResponse } from "@/lib/types";

const MIN_PASSWORD = 12;

export default function AcceptInvitePage() {
  const params = useParams<{ token: string }>();
  const token = typeof params?.token === "string" ? params.token : "";
  const router = useRouter();
  const queryClient = useQueryClient();

  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");

  const preview = useQuery({
    queryKey: ["invitation", token],
    queryFn: () => api<InvitationPreview>(`/api/v1/invitations/${token}`),
    enabled: token !== "",
    retry: false,
  });

  const accept = useMutation({
    mutationFn: () =>
      api<MeResponse>(`/api/v1/invitations/${token}/accept`, {
        method: "POST",
        body: { display_name: displayName, password },
      }),
    onSuccess: (me) => {
      queryClient.setQueryData(["me"], me);
      router.replace("/");
    },
  });

  if (preview.isPending) {
    return (
      <AuthCard title="Checking your invitation" subtitle="One moment.">
        <Spinner />
      </AuthCard>
    );
  }

  if (preview.error || !preview.data) {
    return (
      <AuthCard
        title="This link no longer works"
        subtitle="Invitations are single-use and expire after a week."
        footer={
          <a className="text-accent-strong underline" href="/login">
            Go to sign in
          </a>
        }
      >
        <p className="text-sm text-dim">
          Ask whoever invited you to send a fresh link. If you have already used this one, sign in
          instead.
        </p>
      </AuthCard>
    );
  }

  const invite = preview.data;
  const errorMessage =
    accept.error instanceof ApiError
      ? accept.error.detail
      : accept.error
        ? "Something went wrong — is the API reachable?"
        : null;

  return (
    <AuthCard
      title={`Join ${invite.workspace_name}`}
      subtitle={`You have been invited as ${ROLE_COPY[invite.role].label.toLowerCase()}.`}
      footer={
        <span>
          Already have an account?{" "}
          <a className="text-accent-strong underline" href="/login">
            Sign in
          </a>
        </span>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          accept.mutate();
        }}
      >
        <Field label="Email" hint="This is the address the invitation was sent to.">
          <Input type="email" value={invite.email} readOnly disabled />
        </Field>
        <Field label="Your name">
          <Input
            required
            autoComplete="name"
            maxLength={200}
            value={displayName}
            placeholder="Ada Lovelace"
            onChange={(event) => setDisplayName(event.target.value)}
          />
        </Field>
        <Field
          label="Choose a password"
          hint={`At least ${MIN_PASSWORD} characters. Nobody else ever sees it — not even the person who invited you.`}
        >
          <Input
            type="password"
            required
            minLength={MIN_PASSWORD}
            autoComplete="new-password"
            value={password}
            placeholder="••••••••••••"
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
        <p className="rounded-xl bg-hover px-3 py-2 text-xs text-dim">
          {ROLE_COPY[invite.role].blurb}
        </p>
        <ErrorNote message={errorMessage} />
        <Button type="submit" variant="primary" className="w-full" disabled={accept.isPending}>
          {accept.isPending ? "Creating your account…" : `Join ${invite.workspace_name}`}
        </Button>
      </form>
    </AuthCard>
  );
}
