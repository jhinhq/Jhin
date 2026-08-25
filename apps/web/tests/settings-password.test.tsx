/** The change-password form: what it refuses on its own, what it defers to the
 * API for, and what it tells you after a successful change. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PasswordCard } from "@/components/settings/password-card";
import { api } from "@/lib/api";
import type { WorkspaceRole } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderCard(role: WorkspaceRole = "member") {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <WorkspaceProvider
        user={{
          id: "u1",
          email: "qa@jhin.dev",
          display_name: "QA",
          created_at: "2026-01-01T00:00:00Z",
        }}
        workspace={{
          workspace_id: "w1",
          workspace_name: "Acme",
          workspace_slug: "acme",
          role,
        }}
      >
        <PasswordCard />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

function fill({ current, next, confirm }: { current: string; next: string; confirm: string }) {
  fireEvent.change(screen.getByLabelText("Current password"), { target: { value: current } });
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: next } });
  fireEvent.change(screen.getByLabelText("Confirm new password"), {
    target: { value: confirm },
  });
}

function submitButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: /Change password/ }) as HTMLButtonElement;
}

async function apiError(status: number, detail: string) {
  const { ApiError } = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return new ApiError(status, detail);
}

describe("PasswordCard", () => {
  it("is offered to every signed-in user, not just admins", () => {
    for (const role of ["viewer", "member", "admin", "owner"] as const) {
      renderCard(role);
      expect(screen.getByTestId("password-card")).toBeDefined();
      cleanup();
    }
  });

  it("says up front that every other session ends", () => {
    renderCard();
    expect(screen.getByText(/signs you out everywhere else/)).toBeDefined();
    expect(screen.getByText(/This one stays signed in/)).toBeDefined();
  });

  it("describes the policy before the user picks a password", () => {
    renderCard();
    expect(screen.getByText(/At least 12 characters/)).toBeDefined();
    expect(screen.getByText(/not your email address/)).toBeDefined();
  });

  it("blocks submit while the two new passwords differ", () => {
    renderCard();
    fill({ current: "old-one", next: "orbital-lemon-parade-77", confirm: "orbital-lemon" });
    expect(screen.getByText("The two new passwords do not match.")).toBeDefined();
    expect(submitButton().disabled).toBe(true);
    fireEvent.click(submitButton());
    expect(vi.mocked(api)).not.toHaveBeenCalled();
  });

  it("blocks submit until all three fields are filled", () => {
    renderCard();
    fill({ current: "", next: "orbital-lemon-parade-77", confirm: "orbital-lemon-parade-77" });
    expect(submitButton().disabled).toBe(true);
  });

  it("sends only the two fields the API takes", async () => {
    vi.mocked(api).mockResolvedValue({});
    renderCard();
    fill({
      current: "qa-password-2026",
      next: "orbital-lemon-parade-77",
      confirm: "orbital-lemon-parade-77",
    });
    fireEvent.click(submitButton());

    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/auth/password", {
        method: "POST",
        body: { current_password: "qa-password-2026", new_password: "orbital-lemon-parade-77" },
      }),
    );
  });

  it("shows the API's own words when the policy rejects the password", async () => {
    vi.mocked(api).mockRejectedValue(
      await apiError(422, "Password is one of the most commonly guessed passwords; choose another"),
    );
    renderCard();
    fill({ current: "qa-password-2026", next: "password1234", confirm: "password1234" });
    fireEvent.click(submitButton());

    await waitFor(() =>
      expect(
        screen.getByText(
          "Password is one of the most commonly guessed passwords; choose another",
        ),
      ).toBeDefined(),
    );
    // Marked on the field it is about, not dumped at the bottom of the form.
    expect(
      (screen.getByLabelText("New password") as HTMLInputElement).getAttribute("aria-invalid"),
    ).toBe("true");
  });

  it("shows a wrong current password inline on that field", async () => {
    vi.mocked(api).mockRejectedValue(await apiError(403, "Current password is incorrect"));
    renderCard();
    fill({
      current: "not-my-password",
      next: "orbital-lemon-parade-77",
      confirm: "orbital-lemon-parade-77",
    });
    fireEvent.click(submitButton());

    await waitFor(() => expect(screen.getByText("Current password is incorrect")).toBeDefined());
    expect(
      (screen.getByLabelText("Current password") as HTMLInputElement).getAttribute("aria-invalid"),
    ).toBe("true");
  });

  it("reports the outcome and empties the fields on success", async () => {
    vi.mocked(api).mockResolvedValue({});
    renderCard();
    fill({
      current: "qa-password-2026",
      next: "orbital-lemon-parade-77",
      confirm: "orbital-lemon-parade-77",
    });
    fireEvent.click(submitButton());

    await waitFor(() =>
      expect(screen.getByText(/Every other signed-in device has been signed out/)).toBeDefined(),
    );
    for (const label of ["Current password", "New password", "Confirm new password"]) {
      expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe("");
    }
  });

  it("hides the characters until asked, and never puts them in the DOM as text", () => {
    renderCard();
    fill({ current: "a", next: "b", confirm: "c" });
    const current = screen.getByLabelText("Current password") as HTMLInputElement;
    expect(current.type).toBe("password");

    fireEvent.click(screen.getByRole("button", { name: "Show passwords" }));
    expect((screen.getByLabelText("Current password") as HTMLInputElement).type).toBe("text");

    fireEvent.click(screen.getByRole("button", { name: "Hide passwords" }));
    expect((screen.getByLabelText("Current password") as HTMLInputElement).type).toBe("password");
  });
});
