"use client";

/** Shared pieces for the People and API keys screens: role copy, the one-time
 * secret reveal, the scope tree, and the expiry picker.
 *
 * None of this file knows any scope strings. The tree renders whatever the API
 * serves from the canonical taxonomy (docs/architecture/api-keys.md), so the
 * labels a person reads and the strings the API validates cannot drift. */

import { Check, ChevronRight, Copy, Lock } from "lucide-react";
import { useState } from "react";
import { Badge, Button, Field, focusRing, Input, Select } from "@/components/ui";
import type { ExpiryUnit, ScopeCatalog, WorkspaceRole } from "@/lib/types";

export const ROLE_ORDER: readonly WorkspaceRole[] = ["viewer", "member", "admin", "owner"] as const;

/** Plain language, no jargon: what this role means for the person holding it. */
export const ROLE_COPY: Record<WorkspaceRole, { label: string; blurb: string }> = {
  viewer: {
    label: "Viewer",
    blurb: "Can look at everything and change nothing. Cannot chat with agents or start work.",
  },
  member: {
    label: "Member",
    blurb: "Can chat with agents, give them work, and answer approvals. Cannot change setup.",
  },
  admin: {
    label: "Admin",
    blurb: "Can set up apps, agents, automations, models, and budgets, and invite people.",
  },
  owner: {
    label: "Owner",
    blurb: "Everything an admin can do, plus managing admins and deleting the workspace.",
  },
};

export function RoleBadge({ role }: { role: WorkspaceRole }) {
  const tone = role === "owner" ? "accent" : role === "admin" ? "info" : "neutral";
  return <Badge tone={tone}>{ROLE_COPY[role].label}</Badge>;
}

/** Role picker with the plain-language description under it. `maxRole` hides
 * roles the signed-in person may not hand out (only owners create owners). */
export function RoleSelect({
  value,
  onChange,
  maxRole,
  label = "Role",
  disabled = false,
}: {
  value: WorkspaceRole;
  onChange: (role: WorkspaceRole) => void;
  maxRole: WorkspaceRole;
  label?: string;
  disabled?: boolean;
}) {
  const ceiling = ROLE_ORDER.indexOf(maxRole);
  const options = ROLE_ORDER.filter((role) => ROLE_ORDER.indexOf(role) <= ceiling);
  return (
    <Field label={label} hint={ROLE_COPY[value].blurb}>
      <Select
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value as WorkspaceRole)}
      >
        {options.map((role) => (
          <option key={role} value={role}>
            {ROLE_COPY[role].label}
          </option>
        ))}
      </Select>
    </Field>
  );
}

/** A secret shown exactly once, with a copy button and an unmissable warning.
 * Used for both the invite link and a freshly minted API key. */
export function OneTimeSecret({
  label,
  value,
  warning,
  testId,
}: {
  label: string;
  value: string;
  warning: string;
  testId?: string;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div
      data-testid={testId}
      className="rounded-2xl border border-accent/40 bg-accent-soft p-4"
    >
      <p className="text-sm font-semibold text-accent-strong">{label}</p>
      <p className="mt-1 text-[13px] text-dim">{warning}</p>
      <div className="mt-3 flex items-center gap-2">
        <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded-xl border border-line bg-surface px-3 py-2 font-mono text-[13px]">
          {value}
        </code>
        <Button
          type="button"
          variant="primary"
          onClick={() => {
            void navigator.clipboard?.writeText(value);
            setCopied(true);
          }}
        >
          {copied ? <Check size={14} /> : <Copy size={14} />}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
    </div>
  );
}

/** Category checkboxes that expand into granular toggles. Scopes the signed-in
 * role may not grant stay visible but disabled, with the reason on the row —
 * hiding them would leave people wondering what they are missing. */
export function ScopeTree({
  catalog,
  selected,
  onChange,
}: {
  catalog: ScopeCatalog;
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
}) {
  const [open, setOpen] = useState<Set<string>>(new Set());

  const toggleScope = (key: string) => {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onChange(next);
  };

  return (
    <ul className="space-y-2" data-testid="scope-tree">
      {catalog.categories.map((category) => {
        const grantable = category.scopes.filter((scope) => scope.available);
        const chosen = grantable.filter((scope) => selected.has(scope.key));
        const allChosen = grantable.length > 0 && chosen.length === grantable.length;
        const expanded = open.has(category.key);

        return (
          <li key={category.key} className="rounded-2xl border border-line bg-surface">
            <div className="flex items-center gap-3 px-3 py-2.5">
              <input
                type="checkbox"
                id={`scope-cat-${category.key}`}
                className="h-4 w-4 shrink-0 accent-[var(--accent)]"
                disabled={grantable.length === 0}
                checked={allChosen}
                ref={(node) => {
                  if (node) node.indeterminate = chosen.length > 0 && !allChosen;
                }}
                onChange={(event) => {
                  const next = new Set(selected);
                  for (const scope of grantable) {
                    if (event.target.checked) next.add(scope.key);
                    else next.delete(scope.key);
                  }
                  onChange(next);
                }}
              />
              <label
                htmlFor={`scope-cat-${category.key}`}
                className="min-w-0 flex-1 cursor-pointer"
              >
                <span className="text-sm font-medium">{category.label}</span>
                <span className="ml-2 text-xs text-faint">{category.description}</span>
              </label>
              {chosen.length > 0 ? <Badge tone="accent">{chosen.length}</Badge> : null}
              <button
                type="button"
                aria-expanded={expanded}
                aria-label={`${expanded ? "Hide" : "Show"} ${category.label} permissions`}
                onClick={() => {
                  const next = new Set(open);
                  if (expanded) next.delete(category.key);
                  else next.add(category.key);
                  setOpen(next);
                }}
                className={`inline-flex h-8 w-8 items-center justify-center rounded-lg text-dim hover:bg-hover hover:text-ink ${focusRing}`}
              >
                <ChevronRight
                  size={16}
                  aria-hidden
                  className={`transition-transform ${expanded ? "rotate-90" : ""}`}
                />
              </button>
            </div>

            {expanded ? (
              <ul className="border-t border-line px-3 py-2">
                {category.scopes.map((scope) => (
                  <li key={scope.key} className="flex items-start gap-3 py-1.5">
                    <input
                      type="checkbox"
                      id={`scope-${scope.key}`}
                      className="mt-1 h-4 w-4 shrink-0 accent-[var(--accent)]"
                      disabled={!scope.available}
                      checked={selected.has(scope.key)}
                      onChange={() => toggleScope(scope.key)}
                    />
                    <label
                      htmlFor={`scope-${scope.key}`}
                      className={`min-w-0 flex-1 ${scope.available ? "cursor-pointer" : "opacity-60"}`}
                    >
                      <span className="text-[13px] font-medium">{scope.label}</span>
                      <code className="ml-2 font-mono text-[11px] text-faint">{scope.key}</code>
                      <span className="block text-xs text-dim">{scope.description}</span>
                      {!scope.available ? (
                        <span className="mt-0.5 inline-flex items-center gap-1 text-xs text-warn">
                          <Lock size={11} aria-hidden />
                          Needs the {ROLE_COPY[scope.min_role].label.toLowerCase()} role — a key
                          can never do more than you can.
                        </span>
                      ) : null}
                    </label>
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export const EXPIRY_UNITS: readonly { value: ExpiryUnit; label: string }[] = [
  { value: "minutes", label: "minutes" },
  { value: "hours", label: "hours" },
  { value: "days", label: "days" },
  { value: "never", label: "never expires" },
] as const;

export function ExpiryPicker({
  amount,
  unit,
  onAmountChange,
  onUnitChange,
}: {
  amount: string;
  unit: ExpiryUnit;
  onAmountChange: (value: string) => void;
  onUnitChange: (value: ExpiryUnit) => void;
}) {
  return (
    <div className="flex items-end gap-3">
      {unit === "never" ? null : (
        <div className="w-28">
          <Field label="Expires in">
            <Input
              type="number"
              min="1"
              value={amount}
              aria-label="Expiry amount"
              onChange={(event) => onAmountChange(event.target.value)}
            />
          </Field>
        </div>
      )}
      <div className="w-44">
        <Field label={unit === "never" ? "Expiry" : " "}>
          <Select
            value={unit}
            aria-label="Expiry unit"
            onChange={(event) => onUnitChange(event.target.value as ExpiryUnit)}
          >
            {EXPIRY_UNITS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>
      </div>
    </div>
  );
}
