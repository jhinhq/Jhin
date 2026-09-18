"use client";
import { useState } from "react";
import { Button, Dialog, Field, Input } from "@/components/ui";
import type { SecureChatInput } from "@/lib/private-input";

/** An explicit path for arbitrary credentials that pattern detection cannot recognize. */
export function SecureInputButton({ onSend, disabled }: { onSend: (input: SecureChatInput) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  return <><button type="button" disabled={disabled} onClick={() => setOpen(true)} className="min-h-9 rounded-lg px-2 text-xs text-accent-strong hover:bg-hover disabled:opacity-40">Send a secret</button>
    {open ? <Dialog open title="Send a secret" description="Send this value with your current message. It is encrypted by the server and replaced with a reference before the message is saved." onClose={() => setOpen(false)} footer={<div className="flex justify-end gap-2"><Button onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" form="secure-chat-input" variant="primary">Send securely</Button></div>}>
      <form id="secure-chat-input" autoComplete="off" className="space-y-4" onSubmit={(event) => { event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const input = { name: String(data.get("name") ?? "").trim() || undefined, value: String(data.get("value") ?? "") }; form.reset(); data.delete("value"); setOpen(false); onSend(input); }}>
        <Field label="Secret name (optional)"><Input name="name" maxLength={120} pattern="[A-Za-z_][A-Za-z0-9_.\-]{0,119}" placeholder="GHOST_ADMIN_KEY" /></Field><Field label="Secret value"><Input name="value" type="password" autoComplete="new-password" required maxLength={8192} /></Field>
      </form>
    </Dialog> : null}
  </>;
}
export function SecureInputReceipt({ content }: { content: Record<string, unknown> }) {
  const inputs = Array.isArray(content.secure_inputs) ? content.secure_inputs.filter((item) => item && typeof item === "object" && typeof item.secret_ref === "string") : [];
  if (!inputs.length) return null;
  return <ul aria-label="Saved secrets" className="mt-2 space-y-1 border-t border-accent/20 pt-2 text-xs text-dim">{inputs.map((item, index) => <li key={index}>Secret saved: {typeof item.name === "string" ? item.name : "Private input"}. Value cannot be revealed.</li>)}</ul>;
}
