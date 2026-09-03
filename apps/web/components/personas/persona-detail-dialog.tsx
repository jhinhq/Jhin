"use client";

/** The whole card, read-only: every facet, the block the agent reads, and
 * the admin actions the gallery card offers. */

import { Copy, Pencil, Power } from "lucide-react";
import {
  PersonaSourceBadge,
  PersonaTagBadges,
  SwitchedOffBadge,
} from "@/components/personas/persona-badges";
import { PersonaBlockPreview } from "@/components/personas/persona-block-preview";
import { PersonaFacetList } from "@/components/personas/persona-facets";
import { Button, Dialog } from "@/components/ui";
import { formatDateTime } from "@/lib/format";
import { agentCountText } from "@/lib/personas";
import type { Persona } from "@/lib/types";

export function PersonaDetailDialog({
  persona,
  isAdmin,
  onClose,
  onEdit,
  onDuplicate,
  onToggle,
  busy,
}: {
  persona: Persona;
  isAdmin: boolean;
  onClose: () => void;
  onEdit: () => void;
  onDuplicate: () => void;
  onToggle: () => void;
  busy: boolean;
}) {
  return (
    <Dialog
      wide
      open
      title={persona.display_name}
      description={persona.description}
      onClose={onClose}
    >
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-2">
          <PersonaSourceBadge source={persona.source} />
          {!persona.enabled ? <SwitchedOffBadge /> : null}
          <PersonaTagBadges tags={persona.tags} />
          <span className="text-xs text-faint">{agentCountText(persona.agent_count)}</span>
        </div>
        <PersonaFacetList facets={persona.facets} />
        <div>
          <h3 className="mb-1.5 text-[13px] font-medium text-dim">What the agent reads</h3>
          <PersonaBlockPreview
            name={persona.name}
            displayName={persona.display_name}
            facets={persona.facets}
            audience="both"
          />
        </div>
        <p className="text-xs text-faint">
          Version {persona.version} · Updated {formatDateTime(persona.updated_at)}
          {persona.source === "agent" ? " · Written by an agent and approved by a person." : ""}
        </p>
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          {isAdmin ? (
            <>
              <Button onClick={onDuplicate} disabled={busy}>
                <Copy size={14} /> {persona.read_only ? "Duplicate to make it yours" : "Duplicate"}
              </Button>
              {!persona.read_only ? (
                <Button onClick={onEdit} disabled={busy}>
                  <Pencil size={14} /> Edit
                </Button>
              ) : null}
              <Button onClick={onToggle} disabled={busy}>
                <Power size={14} /> {persona.enabled ? "Disable" : "Enable"}
              </Button>
            </>
          ) : null}
        </div>
      </div>
    </Dialog>
  );
}
