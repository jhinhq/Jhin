"use client";
import { useRef, useState } from "react";
import { Button, Dialog, ErrorNote, Field, Input, Textarea } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { variableBase, type ScopedVariable, type VariableScope } from "@/lib/scoped-variables";

export function VariableForm({ workspaceId, scope, scopeId, item, replaceSecret = false, onClose, onSaved }: {
  workspaceId: string; scope: VariableScope; scopeId: string; item?: ScopedVariable; replaceSecret?: boolean;
  onClose: () => void; onSaved: () => void;
}) {
  const form = useRef<HTMLFormElement>(null);
  const [sensitive, setSensitive] = useState(item?.sensitive ?? false);
  const [busy, setBusy] = useState(false), [stale, setStale] = useState(false), [error, setError] = useState<string | null>(null);
  const secretWrite = replaceSecret || (!item && sensitive);
  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || stale) return;
    const data = new FormData(event.currentTarget);
    const body: Record<string, unknown> = item ? { expected_version: item.version } : { scope, scope_id: scopeId, sensitive };
    if (!replaceSecret) { body.name = String(data.get("name") ?? "").trim(); body.description = String(data.get("description") ?? "").trim(); }
    if (!item || !item.sensitive || replaceSecret) body.value = String(data.get("value") ?? "");
    // Never put a credential in a React Query mutation or restore it after failure.
    if (secretWrite) { const input = form.current?.elements.namedItem("value"); if (input instanceof HTMLInputElement) input.value = ""; }
    setBusy(true); setError(null);
    try {
      const base = variableBase(workspaceId);
      await api(item ? `${base}/${item.id}${replaceSecret ? "/secret" : ""}` : `${base}${sensitive ? "/secrets" : ""}`, { method: item ? replaceSecret ? "PUT" : "PATCH" : "POST", body });
      onSaved(); onClose();
    } catch (failure) {
      const conflict = failure instanceof ApiError && failure.status === 409;
      setStale(conflict);
      // Errors from secret intake must not echo a rejected value.
      setError(conflict ? "This variable changed. Close this form and reload before editing again." : secretWrite ? "Couldn't save the secret. Check your access and connection, then enter the value again." : failure instanceof ApiError ? failure.detail : "Couldn't save this variable. Try again.");
    } finally { delete body.value; data.delete("value"); setBusy(false); }
  };
  return <Dialog open title={replaceSecret ? `Replace ${item?.name}` : item ? "Edit variable" : "Add variable"} onClose={onClose}
    description={secretWrite ? "The value is encrypted when saved and cannot be revealed afterwards." : "Variables stay in the selected scope. Create a new variable to use a different scope."}
    footer={<div className="flex justify-end gap-2"><Button onClick={onClose}>Cancel</Button><Button type="submit" form="variable-form" variant="primary" disabled={busy || stale}>{busy ? "Saving…" : secretWrite ? "Save secret" : "Save variable"}</Button></div>}>
    <form id="variable-form" ref={form} onSubmit={(event) => void submit(event)} className="space-y-4" autoComplete="off">
      <ErrorNote message={error} />
      {!replaceSecret ? <><Field label="Name" hint="Letters, digits, underscores, periods, or hyphens; start with a letter or underscore."><Input aria-label="Name" name="name" required maxLength={120} pattern="[A-Za-z_][A-Za-z0-9_.\-]{0,119}" defaultValue={item?.name} /></Field><Field label="Description"><Textarea aria-label="Description" name="description" maxLength={2000} defaultValue={item?.description ?? ""} rows={2} /></Field></> : null}
      {!item ? <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={sensitive} onChange={(event) => { const input = form.current?.elements.namedItem("value"); if (input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement) input.value = ""; setSensitive(event.target.checked); }} />Sensitive value</label> : null}
      {secretWrite ? <Field label="New secret value"><Input name="value" type="password" autoComplete="new-password" required disabled={busy || stale} /></Field> : !item?.sensitive ? <Field label="Value"><Textarea aria-label="Value" name="value" required defaultValue={item?.value ?? ""} rows={4} /></Field> : <p className="text-sm text-dim">The saved secret stays configured. Use Replace secret to change its value.</p>}
    </form>
  </Dialog>;
}
