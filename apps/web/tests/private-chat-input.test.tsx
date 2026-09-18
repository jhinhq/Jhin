import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, render, screen, fireEvent } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { emptyDraft, readDraft, saveDraft } from "@/lib/agentic-chat";
import { stashCarriedDraft, takeCarriedDraft } from "@/lib/chat";
import { useTransientMutation } from "@/lib/use-transient-mutation";
import { SecureInputButton, SecureInputReceipt } from "@/components/chat/secure-input";
afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); });
const ghost = `${"a".repeat(24)}:${"b".repeat(64)}`;
it.each([ghost, "API key = a-private-value", "password: private value", "Bearer test-token", "-----BEGIN PRIVATE KEY-----\nabc"])("does not persist a secret-bearing draft or carry text", (text) => {
  saveDraft("w", "c", { ...emptyDraft(), text, attachment_ids: ["f1"] });
  expect(JSON.stringify(Array.from({ length: localStorage.length }, (_, i) => localStorage.getItem(localStorage.key(i)!)))).not.toContain(text);
  expect(readDraft("w", "c").attachment_ids).toEqual(["f1"]);
  expect(readDraft("w", "c").text).toBe("");
  stashCarriedDraft("c", text); expect(sessionStorage.length).toBe(0); expect(takeCarriedDraft("c")).toBe(text);
});
it("retains ordinary drafts and attachment versions", () => {
  saveDraft("w", "c", { ...emptyDraft(), text: "Summarize this document", context_refs: [{kind:"file",id:"f",version_id:"v2"}] });
  expect(readDraft("w", "c").text).toBe("Summarize this document"); expect(readDraft("w", "c").context_refs[0].version_id).toBe("v2");
});
it("submits transient input without retaining arguments in any query mutation cache", async () => {
  const client = new QueryClient(); let finish!: (value: string) => void;
  const { result } = renderHook(() => useTransientMutation({ mutationFn: async (_text: string) => new Promise<string>((resolve) => { finish = resolve; }) }), { wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
  act(() => result.current.mutate(ghost)); expect(result.current.isPending).toBe(true); expect(client.getMutationCache().getAll()).toHaveLength(0);
  await act(async () => { finish("[secret captured]"); }); expect(result.current.isPending).toBe(false); expect(client.getQueryCache().getAll()).toHaveLength(0);
});
it("clears the explicit secret form before sending and displays only receipt metadata", () => {
  const onSend = vi.fn(); render(<SecureInputButton onSend={onSend} />);
  fireEvent.click(screen.getByRole("button", {name:"Send a secret"}));
  const input = screen.getByLabelText("Secret value") as HTMLInputElement;
  fireEvent.change(input, {target:{value:"arbitrary-private-value"}});
  fireEvent.click(screen.getByRole("button", {name:"Send securely"}));
  expect(input.value).toBe(""); expect(onSend).toHaveBeenCalledWith({name:undefined,value:"arbitrary-private-value"});
  expect(screen.queryByRole("dialog")).toBeNull();
  render(<SecureInputReceipt content={{secure_inputs:[{secret_ref:"ref",name:"API key",value:"must-not-show"}]}} />);
  expect(screen.getByText(/Secret saved: API key/)).toBeDefined(); expect(document.body.textContent).not.toContain("must-not-show");
});
