/** The shared yes-or-no dialog that replaced `window.confirm`: two buttons,
 * a body that can carry real consequences, and a `busy` state that stops a
 * double-click from firing the confirmed action twice. */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConfirmDialog } from "@/components/ui";

afterEach(cleanup);

function renderDialog(overrides: Partial<Parameters<typeof ConfirmDialog>[0]> = {}) {
  const onConfirm = vi.fn();
  const onClose = vi.fn();
  render(
    <ConfirmDialog
      open
      title="Delete this model?"
      body="Agents using it fall back to the workspace default."
      confirmLabel="Delete"
      onConfirm={onConfirm}
      onClose={onClose}
      {...overrides}
    />,
  );
  return { onConfirm, onClose };
}

describe("ConfirmDialog", () => {
  it("renders nothing while closed", () => {
    renderDialog({ open: false });
    expect(screen.queryByTestId("confirm-dialog")).toBeNull();
  });

  it("confirms and cancels through its two buttons", () => {
    const { onConfirm, onClose } = renderDialog();
    expect(screen.getByRole("dialog", { name: "Delete this model?" })).toBeDefined();
    const dialog = screen.getByTestId("confirm-dialog");
    expect(within(dialog).getByText(/fall back to the workspace default/)).toBeDefined();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("defaults to the danger variant and honours the primary tone", () => {
    renderDialog();
    expect(screen.getByRole("button", { name: "Delete" }).className).toContain("danger");
    cleanup();
    renderDialog({ tone: "primary", confirmLabel: "Make default" });
    expect(screen.getByRole("button", { name: "Make default" }).className).toContain("btn-gradient");
  });

  it("renames the cancel button when asked", () => {
    renderDialog({ cancelLabel: "Keep it" });
    expect(screen.getByRole("button", { name: "Keep it" })).toBeDefined();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("freezes both buttons while busy", () => {
    const { onConfirm, onClose } = renderDialog({ busy: true });
    const dialog = screen.getByTestId("confirm-dialog");
    const confirm = within(dialog).getByRole("button", { name: "Delete" }) as HTMLButtonElement;
    const cancel = within(dialog).getByRole("button", { name: "Cancel" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    expect(cancel.disabled).toBe(true);
    fireEvent.click(confirm);
    fireEvent.click(cancel);
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});
