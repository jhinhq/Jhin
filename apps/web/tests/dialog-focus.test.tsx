/** Dialog focus belongs to the open/close lifecycle, not to form rerenders. */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { Dialog } from "@/components/ui";

afterEach(cleanup);

function DialogHarness() {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [selected, setSelected] = useState(false);
  const [closedWith, setClosedWith] = useState("");

  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open permissions
      </button>
      <output aria-label="Closed values">{closedWith}</output>
      <Dialog
        title="Permissions"
        open={open}
        onClose={() => {
          setClosedWith(`${name}|${notes}|${selected}`);
          setOpen(false);
        }}
      >
        <label>
          Name
          <input value={name} onChange={(event) => setName(event.target.value)} />
        </label>
        <label>
          Read reports
          <input
            type="checkbox"
            checked={selected}
            onChange={(event) => setSelected(event.target.checked)}
          />
        </label>
        <label>
          Notes
          <textarea value={notes} onChange={(event) => setNotes(event.target.value)} />
        </label>
        <button type="button" onClick={() => setOpen(false)}>
          Done
        </button>
      </Dialog>
    </>
  );
}

function openDialog() {
  const trigger = screen.getByRole("button", { name: "Open permissions" });
  // fireEvent does not perform the browser's pointer-induced focus change.
  trigger.focus();
  fireEvent.click(trigger);
  return trigger;
}

describe("Dialog focus lifecycle", () => {
  it("releases the page scroll lock when navigating away with a nested form open", () => {
    document.body.style.overflow = "auto";
    const {unmount}=render(<Dialog open title="Resources" onClose={()=>{}}><Dialog open title="Variable" onClose={()=>{}}><input /></Dialog></Dialog>);
    expect(document.body.style.overflow).toBe("hidden");unmount();expect(document.body.style.overflow).toBe("auto");
    document.body.style.overflow="";
  });
  it("closes only the top form when Escape is pressed inside a company resource dialog", () => {
    function Nested() { const [parent, setParent] = useState(true), [child, setChild] = useState(true); return <Dialog open={parent} title="Resources" onClose={() => setParent(false)}><button>Scope</button>{child ? <Dialog open title="Edit secret" onClose={() => setChild(false)}><input aria-label="Secret" /></Dialog> : null}</Dialog>; }
    render(<Nested />);
    fireEvent.keyDown(screen.getByLabelText("Secret"), {key:"Escape"});
    expect(screen.queryByRole("dialog",{name:"Edit secret"})).toBeNull();
    expect(screen.getByRole("dialog",{name:"Resources"})).toBeDefined();
  });
  it("focuses the first body control when opened and again after reopening", () => {
    render(<DialogHarness />);
    expect(screen.queryByRole("dialog")).toBeNull();
    openDialog();
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Name" }));

    const notes = screen.getByRole("textbox", { name: "Notes" });
    notes.focus();
    fireEvent.keyDown(notes, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();

    openDialog();
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Name" }));
  });

  it("keeps focus on a lower checkbox when its state changes", () => {
    render(<DialogHarness />);
    openDialog();
    const checkbox = screen.getByRole("checkbox", { name: "Read reports" }) as HTMLInputElement;
    checkbox.focus();

    fireEvent.click(checkbox);

    expect(checkbox.checked).toBe(true);
    expect(document.activeElement).toBe(checkbox);
  });

  it("keeps focus in a lower text field while editing rerenders the dialog", () => {
    render(<DialogHarness />);
    openDialog();
    const notes = screen.getByRole("textbox", { name: "Notes" }) as HTMLTextAreaElement;
    notes.focus();

    fireEvent.change(notes, { target: { value: "Keep this report" } });

    expect(notes.value).toBe("Keep this report");
    expect(document.activeElement).toBe(notes);
  });

  it("Escape closes with the latest form values and restores focus to the trigger", () => {
    render(<DialogHarness />);
    const trigger = openDialog();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Latest name" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Notes" }), {
      target: { value: "Latest notes" },
    });
    const checkbox = screen.getByRole("checkbox", { name: "Read reports" });
    checkbox.focus();
    fireEvent.click(checkbox);

    fireEvent.keyDown(checkbox, { key: "Escape" });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByLabelText("Closed values").textContent).toBe("Latest name|Latest notes|true");
    expect(document.activeElement).toBe(trigger);
  });

  it("wraps Tab and Shift+Tab inside the dialog after a form rerender", () => {
    render(<DialogHarness />);
    const trigger = openDialog();
    fireEvent.click(screen.getByRole("checkbox", { name: "Read reports" }));
    const dialog = screen.getByRole("dialog", { name: "Permissions" });
    const first = within(dialog).getByRole("button", { name: "Close" });
    const last = within(dialog).getByRole("button", { name: "Done" });

    last.focus();
    expect(fireEvent.keyDown(last, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(first);

    expect(fireEvent.keyDown(first, { key: "Tab", shiftKey: true })).toBe(false);
    expect(document.activeElement).toBe(last);

    fireEvent.click(last);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
});
