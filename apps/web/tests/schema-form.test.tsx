/** The generic form the Connect dialog renders from a normalised schema.
 *
 * One control per field, each reachable by its own label, so a person using a
 * screen reader meets the same form everybody else does. The interesting cases
 * are the ones where the schema asked for something the form will not give it:
 * a thirty-option select becomes a text input, and a secret never renders with
 * a value in it. */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SchemaForm } from "@/components/schema-form";
import {
  normalizeConfigSchema,
  type ConfigSchema,
  type ConfigSchemaField,
  type ConfigValue,
} from "@/lib/config-schema";

afterEach(cleanup);

function field(overrides: Partial<ConfigSchemaField> = {}): ConfigSchemaField {
  return {
    name: "server_url",
    label: "Server URL",
    type: "string",
    required: false,
    secret: false,
    default: null,
    enum: [],
    max_length: null,
    minimum: null,
    maximum: null,
    placeholder: "",
    help: "",
    multiline: false,
    ...overrides,
  };
}

function build(fields: ConfigSchemaField[]): ConfigSchema {
  const normalized = normalizeConfigSchema({
    version: 1,
    connector_type: "mcp",
    fields,
    auth: { type: "bearer", note: "" },
    degraded: [],
  });
  if (!normalized) throw new Error("fixture schema did not normalise");
  return normalized;
}

function renderForm(fields: ConfigSchemaField[], values: Record<string, ConfigValue> = {}) {
  const onChange = vi.fn();
  const schema = build(fields);
  render(<SchemaForm schema={schema} values={values} onChange={onChange} />);
  return { onChange, schema };
}

describe("SchemaForm", () => {
  it("renders one labelled control per field", () => {
    renderForm([
      field(),
      field({ name: "port", label: "Port", type: "integer" }),
      field({ name: "on", label: "Allow writes", type: "boolean" }),
      field({ name: "hosts", label: "Hosts", type: "string_list" }),
    ]);

    const form = screen.getByTestId("schema-form");
    for (const label of ["Server URL", "Port", "Allow writes", "Hosts"]) {
      expect(within(form).getByLabelText(label)).toBeDefined();
    }
  });

  it("gives a string field a text input and reports what is typed", () => {
    const { onChange } = renderForm([field()], { server_url: "" });
    const input = screen.getByLabelText("Server URL") as HTMLInputElement;

    expect(input.tagName).toBe("INPUT");
    expect(input.type).toBe("text");
    fireEvent.change(input, { target: { value: "https://mcp.example.com/mcp" } });
    expect(onChange).toHaveBeenCalledWith("server_url", "https://mcp.example.com/mcp");
  });

  it("gives an integer field a number input carrying its bounds", () => {
    renderForm(
      [field({ name: "port", label: "Port", type: "integer", minimum: 1, maximum: 65535 })],
      { port: 443 },
    );
    const input = screen.getByLabelText("Port") as HTMLInputElement;

    expect(input.type).toBe("number");
    expect(input.min).toBe("1");
    expect(input.max).toBe("65535");
    expect(input.value).toBe("443");
  });

  it("gives a boolean field a checkbox", () => {
    const { onChange } = renderForm([field({ name: "on", label: "Allow writes", type: "boolean" })], {
      on: false,
    });
    const box = screen.getByLabelText("Allow writes") as HTMLInputElement;

    expect(box.type).toBe("checkbox");
    expect(box.checked).toBe(false);
    fireEvent.click(box);
    expect(onChange).toHaveBeenCalledWith("on", true);
  });

  it("gives a list field a textarea", () => {
    renderForm([field({ name: "hosts", label: "Hosts", type: "string_list" })], {
      hosts: ["a.example.com", "b.example.com"],
    });

    expect((screen.getByLabelText("Hosts") as HTMLTextAreaElement).tagName).toBe("TEXTAREA");
  });

  it("gives a multiline string field a textarea too", () => {
    renderForm([field({ name: "notes", label: "Notes", multiline: true })], { notes: "hi" });

    expect((screen.getByLabelText("Notes") as HTMLTextAreaElement).tagName).toBe("TEXTAREA");
  });

  it("renders an honoured enum as a select with exactly those options", () => {
    renderForm([field({ name: "transport", label: "Transport", enum: ["auto", "streamable_http", "sse"] })], {
      transport: "auto",
    });
    const select = screen.getByLabelText("Transport") as HTMLSelectElement;

    expect(select.tagName).toBe("SELECT");
    expect([...select.options].map((option) => option.value)).toEqual([
      "auto",
      "streamable_http",
      "sse",
    ]);
    expect(select.value).toBe("auto");
  });

  it("falls back to a text input when the enum is too long to be a choice", () => {
    renderForm(
      [
        field({
          name: "region",
          label: "Region",
          enum: Array.from({ length: 30 }, (_, index) => `region-${index}`),
        }),
      ],
      { region: "" },
    );
    const control = screen.getByLabelText("Region") as HTMLInputElement;

    expect(control.tagName).toBe("INPUT");
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("renders a secret as an empty password box", () => {
    renderForm([field({ name: "api_key", label: "API key", secret: true })], {
      api_key: "",
    });
    const input = screen.getByLabelText("API key") as HTMLInputElement;

    expect(input.type).toBe("password");
    expect(input.autocomplete).toBe("off");
    expect(input.value).toBe("");
    expect(screen.getByText(/never displayed again/i)).toBeDefined();
  });

  it("applies max_length to the control the person types into", () => {
    renderForm([field({ max_length: 512 })], { server_url: "" });

    expect((screen.getByLabelText("Server URL") as HTMLInputElement).maxLength).toBe(512);
  });

  it("disables every control when the form is submitting", () => {
    const schema = build([
      field(),
      field({ name: "on", label: "Allow writes", type: "boolean" }),
      field({ name: "transport", label: "Transport", enum: ["auto", "sse"] }),
    ]);
    render(
      <SchemaForm
        schema={schema}
        values={{ server_url: "", on: false, transport: "auto" }}
        onChange={() => {}}
        disabled
      />,
    );

    for (const label of ["Server URL", "Allow writes", "Transport"]) {
      expect((screen.getByLabelText(label) as HTMLInputElement).disabled).toBe(true);
    }
  });

  it("shows the help text and placeholder the schema supplied", () => {
    renderForm(
      [field({ help: "The endpoint from the provider's docs.", placeholder: "https://…" })],
      { server_url: "" },
    );

    expect(screen.getByText("The endpoint from the provider's docs.")).toBeDefined();
    expect((screen.getByLabelText("Server URL") as HTMLInputElement).placeholder).toBe("https://…");
  });

  it("renders nothing hostile from a field's own text", () => {
    renderForm([field({ label: "Server URL", help: "<img src=x onerror=alert(1)>" })], {
      server_url: "",
    });

    const form = screen.getByTestId("schema-form");
    expect(form.querySelector("img")).toBeNull();
    expect(within(form).getByText("<img src=x onerror=alert(1)>")).toBeDefined();
  });
});
