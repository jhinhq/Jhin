"use client";

/** First-run guidance shown when a workspace has no agents yet. An agent
 * cannot work without a model, so the provider step comes first and is
 * marked done once any model profile exists. */

import { CheckCircle2, Circle } from "lucide-react";
import Link from "next/link";
import { Button } from "@/components/ui";
import { useModelProfiles } from "@/lib/hooks";

export function FirstRunSteps({ workspaceId, isAdmin }: { workspaceId: string; isAdmin: boolean }) {
  const profiles = useModelProfiles(workspaceId);
  const hasModel = (profiles.data?.length ?? 0) > 0;

  if (!isAdmin) {
    return <p className="text-xs text-faint">Ask a workspace admin to add one.</p>;
  }

  const steps = [
    {
      done: hasModel,
      title: "Connect a model provider",
      detail: hasModel
        ? "A model is ready for your agents to use."
        : "Agents think with a model — add OpenAI, Anthropic, or any compatible endpoint.",
      href: "/models",
      cta: "Set up a model",
    },
    {
      done: false,
      title: "Create your first agent",
      detail: "Give it a name and a role, then start a chat.",
      href: "/agents/new",
      cta: "Create an agent",
    },
  ];
  const current = steps.find((step) => !step.done) ?? steps[steps.length - 1];

  return (
    <div className="w-full max-w-md space-y-3 text-left">
      <ol className="space-y-2">
        {steps.map((step, index) => {
          const active = step === current;
          return (
            <li
              key={step.href}
              className={`flex items-start gap-3 rounded-xl border px-3.5 py-3 ${
                active ? "border-accent bg-accent-soft/60" : "border-line bg-raised"
              }`}
            >
              {step.done ? (
                <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-ok" aria-label="Done" />
              ) : (
                <Circle size={18} className="mt-0.5 shrink-0 text-faint" aria-hidden />
              )}
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-ink">
                  <span className="text-faint">{index + 1}.</span> {step.title}
                </p>
                <p className="text-xs text-dim">{step.detail}</p>
              </div>
            </li>
          );
        })}
      </ol>
      <div className="flex justify-center">
        <Link href={current.href}>
          <Button variant="primary">{current.cta}</Button>
        </Link>
      </div>
    </div>
  );
}
