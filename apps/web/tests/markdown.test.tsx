/** The shared markdown renderer. Two things are being pinned here: that the
 * six syntax rules produce real elements, and that a link can never carry a
 * scheme that executes. The second matters because the same renderer now draws
 * model output in chat, and model output can be steered by anything the agent
 * read on the way — a fetched page, an MCP tool result, an issue comment. */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Markdown } from "@/components/markdown";
import { isExternalHref, parseInline, parseMarkdown, safeHref } from "@/lib/markdown";

afterEach(cleanup);

describe("safeHref", () => {
  it("passes the schemes a click may open", () => {
    expect(safeHref("https://jhin.dev/docs")).toBe("https://jhin.dev/docs");
    expect(safeHref("http://jhin.dev")).toBe("http://jhin.dev");
    expect(safeHref("mailto:qa@jhin.dev")).toBe("mailto:qa@jhin.dev");
    expect(safeHref("HTTPS://jhin.dev")).toBe("HTTPS://jhin.dev");
  });

  it("passes references that can only point back at this app", () => {
    expect(safeHref("/models")).toBe("/models");
    expect(safeHref("./nested")).toBe("./nested");
    expect(safeHref("../up")).toBe("../up");
    expect(safeHref("#anchor")).toBe("#anchor");
    expect(safeHref("?q=1")).toBe("?q=1");
  });

  it("refuses every scheme that is not on the list", () => {
    for (const href of [
      "javascript:alert(1)",
      "JavaScript:alert(1)",
      "  javascript:alert(1)",
      "data:text/html;base64,PHNjcmlwdD4=",
      "vbscript:msgbox(1)",
      "file:///etc/passwd",
      "ms-msdt:/id",
    ]) {
      expect(safeHref(href)).toBeNull();
    }
  });

  it("refuses a scheme hidden behind characters the browser throws away", () => {
    // A browser strips control characters and whitespace before it decides
    // what scheme a URL has, so these all navigate as `javascript:`.
    for (const sneaky of [
      "java\nscript:alert(1)",
      "java\tscript:alert(1)",
      "java\u0000script:alert(1)",
      "java\u000bscript:alert(1)",
      "java\u00a0script:alert(1)",
      "j a v a s c r i p t:alert(1)",
      "  JaVaScRiPt\n:alert(1)",
    ]) {
      expect(safeHref(sneaky)).toBeNull();
    }
  });

  it("refuses scheme-relative and bare-host references", () => {
    expect(safeHref("//evil.com")).toBeNull();
    expect(safeHref("/\t/evil.com")).toBeNull();
    expect(safeHref("evil.com")).toBeNull();
    expect(safeHref("&#106;avascript:alert(1)")).toBeNull();
    expect(safeHref("   ")).toBeNull();
  });

  it("knows which links leave the app", () => {
    expect(isExternalHref("https://jhin.dev")).toBe(true);
    expect(isExternalHref("mailto:qa@jhin.dev")).toBe(true);
    expect(isExternalHref("/models")).toBe(false);
    expect(isExternalHref("#anchor")).toBe(false);
  });
});

describe("parsing", () => {
  it("keeps code, bold, italics, and links apart from prose", () => {
    expect(parseInline("Send `Bearer x` **now**, *really* [docs](https://d)")).toEqual([
      { kind: "text", text: "Send " },
      { kind: "code", text: "Bearer x" },
      { kind: "text", text: " " },
      { kind: "strong", text: "now" },
      { kind: "text", text: ", " },
      { kind: "em", text: "really" },
      { kind: "text", text: " " },
      { kind: "link", text: "docs", href: "https://d" },
    ]);
  });

  it("degrades an unsafe link to its literal source, hiding nothing", () => {
    expect(parseInline("click [here](javascript:alert(1))")).toEqual([
      // The whole attempt survives as text, target included, so a reader can
      // see what it pointed at.
      { kind: "text", text: "click [here](javascript:alert(1))" },
    ]);
  });

  it("reads headings, paragraphs, both kinds of list, and fenced code", () => {
    const blocks = parseMarkdown(
      "## Auth\n\nUse a key.\n\n- one\n- two\n\n1. first\n2. second\n\n```\ncurl x\n```\n",
    );
    expect(blocks.map((block) => block.kind)).toEqual([
      "heading",
      "paragraph",
      "list",
      "list",
      "code",
    ]);
    expect(blocks[2]).toMatchObject({ ordered: false });
    expect(blocks[3]).toMatchObject({ ordered: true });
    expect(blocks[4]).toEqual({ kind: "code", text: "curl x" });
  });

  it("closes a fence the writer never closed", () => {
    const blocks = parseMarkdown("Here:\n\n```py\nprint(1)");
    expect(blocks.map((block) => block.kind)).toEqual(["paragraph", "code"]);
  });
});

describe("<Markdown>", () => {
  it("renders the marks as elements, not as characters", () => {
    const { container } = render(
      <Markdown source={"**CTO** and *maybe* `code`\n\n- a\n- b\n\n```\nrun\n```\n\n## Head"} />,
    );
    expect(container.querySelector("strong")?.textContent).toBe("CTO");
    expect(container.querySelector("em")?.textContent).toBe("maybe");
    expect(container.querySelector("code")?.textContent).toBe("code");
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("pre")?.textContent).toBe("run");
    expect(container.querySelector("h3")?.textContent).toBe("Head");
    expect(container.textContent).not.toContain("**");
  });

  it("renders a safe link as an anchor that cannot leak the referrer", () => {
    render(<Markdown source="see [docs](https://jhin.dev/docs)" />);
    const link = screen.getByRole("link", { name: "docs" });
    expect(link.getAttribute("href")).toBe("https://jhin.dev/docs");
    expect(link.getAttribute("rel")).toBe("noreferrer noopener");
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("keeps an in-app link in the current tab", () => {
    render(<Markdown source="see [models](/models)" />);
    expect(screen.getByRole("link", { name: "models" }).getAttribute("target")).toBeNull();
  });

  it("renders an unsafe link as plain text, with no anchor at all", () => {
    for (const source of [
      "[x](javascript:alert(1))",
      "[x](JAVASCRIPT:alert(1))",
      "[x](java\nscript:alert(1))",
      "[x](data:text/html,<script>alert(1)</script>)",
      "[x](//evil.com)",
    ]) {
      const { container } = render(<Markdown source={source} />);
      expect(container.querySelector("a")).toBeNull();
      cleanup();
    }
  });

  it("shows raw HTML in the source as text instead of parsing it", () => {
    const { container } = render(
      <Markdown source={'<img src=x onerror="alert(1)"> and <b>bold</b>'} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
    expect(container.textContent).toContain("<b>bold</b>");
  });

  it("gives the chat variant an inherited colour and a scrollable code block", () => {
    const { container } = render(<Markdown source={"```\nx\n```"} variant="chat" />);
    const root = container.firstElementChild!;
    // Docs prose is `text-dim`; inside a bubble the text takes the bubble's.
    expect(root.className).not.toContain("text-dim");
    expect(container.querySelector("pre")!.className).toContain("overflow-x-auto");
  });
});
