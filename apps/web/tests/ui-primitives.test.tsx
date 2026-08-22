/** Primitive tests: theme toggle, dialog focus trap, tabs keyboard nav. */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Button, Dialog, Tabs, ThemeToggle } from "@/components/ui";

afterEach(() => {
  cleanup();
  document.documentElement.classList.remove("dark");
  localStorage.clear();
});

describe("ThemeToggle", () => {
  beforeEach(() => {
    document.documentElement.classList.remove("dark");
  });

  it("toggles the dark class and persists to localStorage", async () => {
    render(<ThemeToggle />);
    const button = screen.getByRole("button", { name: "Switch to dark theme" });
    expect(button.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(button);
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("jhin-theme")).toBe("dark");
    // The label follows the class via a MutationObserver (async).
    const lightButton = await screen.findByRole("button", { name: "Switch to light theme" });
    expect(lightButton.getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(lightButton);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(localStorage.getItem("jhin-theme")).toBe("light");
    await screen.findByRole("button", { name: "Switch to dark theme" });
  });

  it("reflects a pre-set dark class", () => {
    document.documentElement.classList.add("dark");
    render(<ThemeToggle showLabel />);
    expect(screen.getByRole("button", { name: "Switch to light theme" }).textContent).toContain(
      "Dark",
    );
  });
});

function DialogHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      <Dialog title="Edit thing" open={open} onClose={() => setOpen(false)}>
        <input aria-label="First" />
        <Button>Second</Button>
      </Dialog>
    </>
  );
}

describe("Dialog", () => {
  it("is modal, traps focus, and returns focus on close", () => {
    render(<DialogHarness />);
    const opener = screen.getByText("Open");
    opener.focus();
    fireEvent.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Edit thing" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");

    const first = screen.getByLabelText("First");
    expect(document.activeElement).toBe(first);

    // DOM order inside the panel: Close (header) → First → Second.
    // Tab on the last element wraps to the first; Shift+Tab wraps back.
    const second = screen.getByText("Second");
    const close = screen.getByRole("button", { name: "Close" });
    second.focus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(close);

    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(second);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});

function TabsHarness() {
  const [value, setValue] = useState("a");
  return (
    <Tabs
      label="Sections"
      value={value}
      onChange={setValue}
      tabs={[
        { id: "a", label: "Alpha" },
        { id: "b", label: "Beta" },
        { id: "c", label: "Gamma", disabled: true },
        { id: "d", label: "Delta" },
      ]}
    />
  );
}

describe("Tabs", () => {
  it("uses a roving tabindex and arrow keys, skipping disabled tabs", () => {
    render(<TabsHarness />);
    const alpha = screen.getByRole("tab", { name: "Alpha" });
    const beta = screen.getByRole("tab", { name: "Beta" });
    const delta = screen.getByRole("tab", { name: "Delta" });

    expect(alpha.getAttribute("aria-selected")).toBe("true");
    expect(alpha.tabIndex).toBe(0);
    expect(beta.tabIndex).toBe(-1);

    alpha.focus();
    fireEvent.keyDown(alpha, { key: "ArrowRight" });
    expect(beta.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(beta);

    // Gamma is disabled, so ArrowRight jumps to Delta.
    fireEvent.keyDown(beta, { key: "ArrowRight" });
    expect(delta.getAttribute("aria-selected")).toBe("true");
    expect(delta.tabIndex).toBe(0);
    expect(alpha.tabIndex).toBe(-1);

    // Wraps around.
    fireEvent.keyDown(delta, { key: "ArrowRight" });
    expect(alpha.getAttribute("aria-selected")).toBe("true");

    fireEvent.keyDown(alpha, { key: "End" });
    expect(delta.getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(delta, { key: "Home" });
    expect(alpha.getAttribute("aria-selected")).toBe("true");

    fireEvent.click(beta);
    expect(beta.getAttribute("aria-selected")).toBe("true");
  });
});
