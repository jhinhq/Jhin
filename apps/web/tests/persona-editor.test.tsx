/** The persona editor: live counts and preview, the client-side caps that
 * hold the save button, what create and edit send, and where a server
 * refusal lands. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PersonaEditorDialog } from "@/components/personas/persona-editor-dialog";
import { api, ApiError } from "@/lib/api";
import type { Persona } from "@/lib/types";
import { MISSION_CONTROL_FACETS, persona } from "./helpers/personas";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderEditor(initial: Persona | null = null) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <PersonaEditorDialog workspaceId="w1" initial={initial} onClose={onClose} onSaved={onSaved} />
    </QueryClientProvider>,
  );
  return { onClose, onSaved };
}

const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

const saveButton = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

function fillMinimum() {
  type("Name", "house-style");
  type("Display name", "House Style");
  type("Description", "How we sound.");
  type("Voice", "Plain and warm.");
}

describe("PersonaEditorDialog counts and preview", () => {
  it("counts each facet as you type", () => {
    renderEditor();
    const voice = "Plain, confident, unhurried. Says the answer and then why";
    expect(voice.length).toBe(57);
    type("Voice", voice);
    expect(screen.getByTestId("count-voice").textContent).toBe("57/240");
    expect(screen.getByTestId("card-total").textContent).toBe("57 / 1,500 characters across the facets");
  });

  it("re-renders the block the agent will read on every keystroke", () => {
    renderEditor();
    type("Display name", "House Style");
    type("Voice", "Plain and warm.");
    const block = screen.getByTestId("persona-block-preview").textContent ?? "";
    expect(block).toContain("How you work — House Style");
    expect(block).toContain("- Voice: Plain and warm.");
    type("Voice", "Plain, warm, and quick.");
    expect(screen.getByTestId("persona-block-preview").textContent).toContain(
      "- Voice: Plain, warm, and quick.",
    );
  });
});

describe("PersonaEditorDialog caps", () => {
  it("holds Create until the name, display name, description, and voice are in", () => {
    renderEditor();
    expect(saveButton("Create persona").disabled).toBe(true);
    type("Name", "house-style");
    type("Display name", "House Style");
    type("Description", "How we sound.");
    expect(saveButton("Create persona").disabled).toBe(true);
    type("Voice", "Plain and warm.");
    expect(saveButton("Create persona").disabled).toBe(false);
  });

  it("stops at six never items", () => {
    renderEditor();
    const add = () => screen.getByRole("button", { name: "Add another" }) as HTMLButtonElement;
    for (let i = 0; i < 5; i += 1) fireEvent.click(add());
    expect(screen.getAllByLabelText(/^Never item \d$/)).toHaveLength(6);
    expect(add().disabled).toBe(true);
    expect(screen.getByText("6 of 6")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove never item 6" }));
    expect(add().disabled).toBe(false);
  });

  it("refuses an over-cap facet and says which one", () => {
    renderEditor();
    fillMinimum();
    expect(saveButton("Create persona").disabled).toBe(false);
    type("Voice", "x".repeat(241));
    expect(screen.getByRole("alert").textContent).toBe("Keep Voice to 240 characters.");
    expect(screen.getByTestId("count-voice").className).toContain("text-danger");
    expect(saveButton("Create persona").disabled).toBe(true);
  });
});

describe("PersonaEditorDialog saving", () => {
  it("creates with the full card, tags parsed and blank never rows dropped", async () => {
    vi.mocked(api).mockResolvedValue(persona());
    const { onClose, onSaved } = renderEditor();
    fillMinimum();
    type("Tags", "Fun, direct, fun");
    type("Never item 1", "  Ramble  ");
    fireEvent.click(screen.getByRole("button", { name: "Add another" }));
    fireEvent.click(saveButton("Create persona"));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/personas", {
        method: "POST",
        body: {
          name: "house-style",
          display_name: "House Style",
          description: "How we sound.",
          tags: ["fun", "direct"],
          facets: {
            voice: "Plain and warm.",
            stance: "",
            pace: "",
            when_unsure: "",
            with_people: "",
            with_teammates: "",
            signature: "",
            never: ["Ramble"],
          },
        },
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(onSaved).toHaveBeenCalled();
  });

  it("edits with a PATCH that never carries the name or the switch", async () => {
    vi.mocked(api).mockResolvedValue(persona());
    const { onClose } = renderEditor(persona({ source: "custom", read_only: false }));
    expect(screen.getByRole("dialog", { name: "Edit persona" })).toBeTruthy();
    expect((screen.getByLabelText("Name") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("Changes reach every agent wearing it on their next run.")).toBeTruthy();
    type("Display name", "Flight Director");
    fireEvent.click(screen.getByRole("button", { name: "Add another" }));
    fireEvent.click(saveButton("Save changes"));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/personas/p1", {
        method: "PATCH",
        body: {
          display_name: "Flight Director",
          description: "Calm flight-director cadence: status, go/no-go, next call.",
          tags: ["fun", "calm", "operations"],
          facets: MISSION_CONTROL_FACETS,
        },
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("puts a content-rule refusal under the facet it names", async () => {
    vi.mocked(api).mockRejectedValue(
      new ApiError(422, "facets.voice: Value error, voice must not name a tool", null, [
        {
          loc: ["body", "facets", "voice"],
          msg: "Value error, voice must not name a tool; a persona shapes how an agent sounds, not what it calls ('skills.read')",
          type: "value_error",
        },
      ]),
    );
    const { onClose } = renderEditor();
    fillMinimum();
    fireEvent.click(saveButton("Create persona"));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(
      "voice must not name a tool; a persona shapes how an agent sounds, not what it calls ('skills.read')",
    );
    expect(alert.parentElement?.parentElement?.textContent).toContain("Voice");
    expect(onClose).not.toHaveBeenCalled();
    // Typing in the facet clears what the server said about it.
    type("Voice", "Plain and warmer.");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("puts a taken name under the name field", async () => {
    vi.mocked(api).mockRejectedValue(
      new ApiError(409, "a persona named 'house-style' already exists"),
    );
    renderEditor();
    fillMinimum();
    fireEvent.click(saveButton("Create persona"));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("a persona named 'house-style' already exists");
    expect(alert.closest("label")?.textContent).toContain("Name");
  });

  it("shows anything else above the buttons", async () => {
    vi.mocked(api).mockRejectedValue(new Error("network down"));
    renderEditor();
    fillMinimum();
    fireEvent.click(saveButton("Create persona"));
    expect((await screen.findByRole("alert")).textContent).toBe("Saving the persona failed.");
  });
});
