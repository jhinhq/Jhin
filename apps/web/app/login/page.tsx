"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { AuthCard } from "@/components/auth-card";
import { Button, ErrorNote, Field, Input } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useBootstrapStatus } from "@/lib/hooks";
import type { MeResponse } from "@/lib/types";

export default function LoginPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const bootstrap = useBootstrapStatus();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  // First run: no accounts exist yet, send the user to owner setup.
  useEffect(() => {
    if (bootstrap.data?.needs_bootstrap) router.replace("/setup");
  }, [bootstrap.data, router]);

  const login = useMutation({
    mutationFn: () =>
      api<MeResponse>("/api/v1/auth/login", {
        method: "POST",
        body: { email, password },
      }),
    onSuccess: (me) => {
      queryClient.setQueryData(["me"], me);
      router.replace("/");
    },
  });

  const errorMessage =
    login.error instanceof ApiError
      ? login.error.detail
      : login.error
        ? "Login failed — is the API reachable?"
        : null;

  return (
    <AuthCard title="Sign in to Jhin" subtitle="Operate your AI organization">
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          login.mutate();
        }}
      >
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
        <Field label="Password">
          <Input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="••••••••••••"
          />
        </Field>
        <ErrorNote message={errorMessage} />
        <Button type="submit" variant="primary" className="w-full" disabled={login.isPending}>
          {login.isPending ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </AuthCard>
  );
}
