"use client";

/** Change your own account password. Every signed-in user gets this, not just
 * admins — it is an account setting that happens to live on the workspace's
 * settings page.
 *
 * The policy is not re-implemented here. The API owns it
 * (`jhin_api/security/passwords.py`), and a rejected password is shown using
 * the API's own sentence, so the two can never drift into telling the user
 * different things. The hint below the field describes the same rules in
 * advance; the only rule enforced in the browser is that the two new-password
 * fields match, because the API has no second field to compare.
 */

import { useMutation } from "@tanstack/react-query";
import { Eye, EyeOff } from "lucide-react";
import { useState } from "react";
import { Button, ErrorNote, Field, Input, focusRing } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace-context";

/** Mirrors `MIN_PASSWORD_LENGTH` in the API. Used only for the hint and the
 *  live character counter — never to block a submit, so that if the API ever
 *  raises its floor the browser quietly defers to it instead of contradicting
 *  it. */
const MIN_PASSWORD_LENGTH = 12;

const POLICY_HINT =
  `At least ${MIN_PASSWORD_LENGTH} characters. Not one of the most commonly guessed ` +
  "passwords, and not your email address.";

export function PasswordCard() {
  const { user } = useWorkspace();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [visible, setVisible] = useState(false);
  const [done, setDone] = useState(false);

  const change = useMutation({
    mutationFn: (payload: { current_password: string; new_password: string }) =>
      api<unknown>("/api/v1/auth/password", { method: "POST", body: payload }),
    onSuccess: () => {
      // Nothing about the password is kept once it has been sent.
      setCurrent("");
      setNext("");
      setConfirm("");
      setVisible(false);
      setDone(true);
    },
  });

  const mismatch = confirm.length > 0 && next !== confirm;
  const complete = current.length > 0 && next.length > 0 && confirm.length > 0;
  const error = change.error instanceof ApiError ? change.error : null;
  // 403 is the API's answer to a wrong current password (it also answers a
  // stale CSRF token with 403, but the client retries that transparently
  // before the error ever reaches here).
  const currentPasswordError = error?.status === 403 ? error.detail : null;
  // 422 is the password policy — shown verbatim, because the API is the only
  // thing that knows which rule was broken.
  const policyError = error?.status === 422 ? error.detail : null;
  const otherError =
    error && !currentPasswordError && !policyError
      ? error.detail
      : change.error && !error
        ? "The password could not be changed."
        : null;

  return (
    <section
      data-testid="password-card"
      className="rounded-2xl border border-line bg-surface p-5 shadow-card"
    >
      <h2 className="mb-1 font-display text-base font-semibold">Password</h2>
      <p className="mb-4 text-sm text-dim">
        Changing your password signs you out everywhere else — every other browser, device and
        desktop session ends immediately. This one stays signed in.
      </p>
      <form
        className="max-w-sm space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!complete || mismatch || change.isPending) return;
          setDone(false);
          change.mutate({ current_password: current, new_password: next });
        }}
      >
        <div>
          <Field label="Current password">
            <Input
              type={visible ? "text" : "password"}
              value={current}
              // Named explicitly: `Field` folds its hint into the wrapping
              // label, which would otherwise make the accessible name a
              // paragraph.
              aria-label="Current password"
              autoComplete="current-password"
              disabled={change.isPending}
              aria-invalid={currentPasswordError ? true : undefined}
              onChange={(event) => setCurrent(event.target.value)}
            />
          </Field>
          {currentPasswordError ? (
            <p role="alert" className="mt-1.5 text-[13px] text-danger">
              {currentPasswordError}
            </p>
          ) : null}
        </div>

        <div>
          <Field label="New password" hint={POLICY_HINT}>
            <Input
              type={visible ? "text" : "password"}
              value={next}
              aria-label="New password"
              // `new-password` asks the password manager to offer a generated
              // one rather than autofilling the old one.
              autoComplete="new-password"
              disabled={change.isPending}
              aria-invalid={policyError ? true : undefined}
              aria-describedby="password-policy-state"
              onChange={(event) => setNext(event.target.value)}
            />
          </Field>
          <p id="password-policy-state" className="mt-1.5 text-[13px]">
            {policyError ? (
              <span role="alert" className="text-danger">
                {policyError}
              </span>
            ) : next.length > 0 && next.length < MIN_PASSWORD_LENGTH ? (
              <span className="text-faint">
                {MIN_PASSWORD_LENGTH - next.length} more characters to go.
              </span>
            ) : null}
          </p>
        </div>

        <div>
          <Field label="Confirm new password">
            <Input
              type={visible ? "text" : "password"}
              value={confirm}
              aria-label="Confirm new password"
              autoComplete="new-password"
              disabled={change.isPending}
              aria-invalid={mismatch ? true : undefined}
              onChange={(event) => setConfirm(event.target.value)}
            />
          </Field>
          {mismatch ? (
            <p role="alert" className="mt-1.5 text-[13px] text-danger">
              The two new passwords do not match.
            </p>
          ) : null}
        </div>

        <button
          type="button"
          aria-pressed={visible}
          onClick={() => setVisible((shown) => !shown)}
          className={`inline-flex items-center gap-1.5 rounded-lg text-[13px] text-dim hover:text-ink ${focusRing}`}
        >
          {visible ? <EyeOff size={14} aria-hidden /> : <Eye size={14} aria-hidden />}
          {visible ? "Hide passwords" : "Show passwords"}
        </button>

        <ErrorNote message={otherError} />

        {done ? (
          <p
            role="status"
            className="rounded-xl border border-line bg-bg px-3.5 py-2.5 text-sm text-dim"
          >
            Password changed for {user.email}. Every other signed-in device has been signed out;
            you are still signed in here.
          </p>
        ) : null}

        <div>
          <Button
            type="submit"
            variant="primary"
            disabled={!complete || mismatch || change.isPending}
          >
            {change.isPending ? "Changing…" : "Change password"}
          </Button>
        </div>
      </form>
    </section>
  );
}
