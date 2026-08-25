import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ExpiryPicker,
  OneTimeSecret,
  ROLE_COPY,
  RoleSelect,
  ScopeTree,
} from "@/components/access";
import type { ExpiryUnit, ScopeCatalog, WorkspaceRole } from "@/lib/types";

afterEach(cleanup);

const CATALOG: ScopeCatalog = {
  your_role: "member",
  categories: [
    {
      key: "chats",
      label: "Chats",
      description: "Conversations between people and agents.",
      scopes: [
        {
          key: "chats:read",
          category: "chats",
          action: "read",
          label: "Read chats",
          description: "Read conversations and their messages.",
          min_role: "viewer",
          available: true,
        },
        {
          key: "chats:write",
          category: "chats",
          action: "write",
          label: "Chat with agents",
          description: "Start conversations and send messages.",
          min_role: "member",
          available: true,
        },
      ],
    },
    {
      key: "audit",
      label: "Audit log",
      description: "The permanent record of who changed what.",
      scopes: [
        {
          key: "audit:read",
          category: "audit",
          action: "read",
          label: "Read the audit log",
          description: "Read the permanent record of who changed what.",
          min_role: "admin",
          available: false,
        },
      ],
    },
  ],
};

function TreeHarness({ initial = [] as string[] }) {
  const [selected, setSelected] = useState(new Set(initial));
  return <ScopeTree catalog={CATALOG} selected={selected} onChange={setSelected} />;
}

describe("ScopeTree", () => {
  it("renders one branch per category, collapsed, with no scope strings invented locally", () => {
    render(<TreeHarness />);

    expect(screen.getByRole("checkbox", { name: /Chats/ })).toBeDefined();
    expect(screen.getByRole("checkbox", { name: /Audit log/ })).toBeDefined();
    // Granular toggles stay hidden until the branch is expanded.
    expect(screen.queryByRole("checkbox", { name: /Read chats/ })).toBeNull();
  });

  it("expands a category into its granular permissions with plain-language copy", () => {
    render(<TreeHarness />);
    fireEvent.click(screen.getByRole("button", { name: /Show Chats permissions/ }));

    expect(screen.getByRole("checkbox", { name: /Read chats/ })).toBeDefined();
    expect(screen.getByRole("checkbox", { name: /Chat with agents/ })).toBeDefined();
    expect(screen.getByText("chats:write")).toBeDefined();
    expect(screen.getByText("Start conversations and send messages.")).toBeDefined();
  });

  it("selects every grantable scope in a category from the category checkbox", () => {
    render(<TreeHarness />);
    fireEvent.click(screen.getByRole("checkbox", { name: /Chats/ }));
    fireEvent.click(screen.getByRole("button", { name: /Show Chats permissions/ }));

    expect((screen.getByRole("checkbox", { name: /Read chats/ }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole("checkbox", { name: /Chat with agents/ }) as HTMLInputElement).checked).toBe(true);
  });

  it("shows scopes above the caller's role as disabled, with the reason", () => {
    render(<TreeHarness />);
    fireEvent.click(screen.getByRole("button", { name: /Show Audit log permissions/ }));

    const toggle = screen.getByRole("checkbox", { name: /Read the audit log/ }) as HTMLInputElement;
    expect(toggle.disabled).toBe(true);
    expect(screen.getByText(/Needs the admin role/)).toBeDefined();
    // The category checkbox has nothing grantable under it, so it is dead too.
    expect((screen.getByRole("checkbox", { name: /Audit log/ }) as HTMLInputElement).disabled).toBe(true);
  });

  it("deselects a single scope without clearing its siblings", () => {
    render(<TreeHarness initial={["chats:read", "chats:write"]} />);
    fireEvent.click(screen.getByRole("button", { name: /Show Chats permissions/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Read chats/ }));

    expect((screen.getByRole("checkbox", { name: /Read chats/ }) as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole("checkbox", { name: /Chat with agents/ }) as HTMLInputElement).checked).toBe(true);
  });
});

describe("OneTimeSecret", () => {
  it("shows the value, the warning, and copies to the clipboard once asked", () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    render(
      <OneTimeSecret
        testId="secret"
        label="Nightly script"
        value="jhin_abcd1234_secret-part"
        warning="This is the only time the key is shown."
      />,
    );

    expect(screen.getByText("jhin_abcd1234_secret-part")).toBeDefined();
    expect(screen.getByText("This is the only time the key is shown.")).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: /Copy/ }));
    expect(writeText).toHaveBeenCalledWith("jhin_abcd1234_secret-part");
    expect(screen.getByRole("button", { name: /Copied/ })).toBeDefined();
  });
});

describe("ExpiryPicker", () => {
  function Harness({ start = "days" as ExpiryUnit }) {
    const [amount, setAmount] = useState("90");
    const [unit, setUnit] = useState<ExpiryUnit>(start);
    return (
      <ExpiryPicker
        amount={amount}
        unit={unit}
        onAmountChange={setAmount}
        onUnitChange={setUnit}
      />
    );
  }

  it("offers minutes, hours, days, and never", () => {
    render(<Harness />);
    const unit = screen.getByRole("combobox", { name: "Expiry unit" }) as HTMLSelectElement;
    expect([...unit.options].map((option) => option.value)).toEqual([
      "minutes",
      "hours",
      "days",
      "never",
    ]);
  });

  it("hides the amount box when the key never expires", () => {
    render(<Harness />);
    expect(screen.getByRole("spinbutton", { name: "Expiry amount" })).toBeDefined();

    fireEvent.change(screen.getByRole("combobox", { name: "Expiry unit" }), { target: { value: "never" } });
    expect(screen.queryByRole("spinbutton", { name: "Expiry amount" })).toBeNull();
  });
});

describe("RoleSelect", () => {
  it("explains the selected role in plain language", () => {
    render(<RoleSelect value="member" onChange={() => {}} maxRole="admin" />);
    expect(screen.getByText(ROLE_COPY.member.blurb)).toBeDefined();
  });

  it("does not offer roles above the ceiling the caller may grant", () => {
    render(<RoleSelect value="member" onChange={() => {}} maxRole="admin" />);
    const select = screen.getByRole("combobox", { name: /Role/ }) as HTMLSelectElement;
    const values = [...select.options].map((option) => option.value as WorkspaceRole);
    expect(values).toEqual(["viewer", "member", "admin"]);
    expect(values).not.toContain("owner");
  });

  it("offers owner when an owner is doing the inviting", () => {
    render(<RoleSelect value="member" onChange={() => {}} maxRole="owner" />);
    const select = screen.getByRole("combobox", { name: /Role/ }) as HTMLSelectElement;
    expect([...select.options].map((option) => option.value)).toContain("owner");
  });
});
