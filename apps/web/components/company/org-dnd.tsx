"use client";

/**
 * Drag-and-drop reorganisation for the Company map.
 *
 * Built on @dnd-kit/core: it ships a keyboard sensor and a screen-reader live
 * region out of the box, and it never touches the HTML5 drag-and-drop API,
 * which is unusable on touch and effectively invisible to assistive tech.
 *
 * Two drop targets exist, and their meanings are fixed (see lib/org-move.ts):
 * an agent row means "report to this agent, and join their team", a team or
 * the Independent pool means "join this group at its top level, with no
 * manager". Illegal drops (self, or onto your own subordinate) are refused
 * here — the target is drawn as rejected while the pointer is still over it,
 * so no doomed request is ever sent.
 */

import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  MeasuringStrategy,
  PointerSensor,
  pointerWithin,
  rectIntersection,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type Announcements,
  type CollisionDetection,
  type DragEndEvent,
  type DragStartEvent,
  type DraggableAttributes,
  type DraggableSyntheticListeners,
} from "@dnd-kit/core";
import { GripVertical } from "lucide-react";
import { createContext, useContext, useMemo, useState } from "react";
import { MoveMenu } from "@/components/company/move-menu";
import type { OrgMoveApi } from "@/components/org/tree";
import { focusRing } from "@/components/ui";
import { canMove, resolveMove, type MoveGraph, type MoveTarget } from "@/lib/org-move";
import type { OrgAgentNode } from "@/lib/types";

const AGENT_PREFIX = "agent:";
const GROUP_PREFIX = "group:";
const NO_TEAM = "none";

export function groupDroppableId(teamId: string | null): string {
  return `${GROUP_PREFIX}${teamId ?? NO_TEAM}`;
}

/** Decode a droppable id back into the move it means. */
export function targetFromDroppableId(id: string): MoveTarget | null {
  if (id.startsWith(AGENT_PREFIX)) return { kind: "agent", agentId: id.slice(AGENT_PREFIX.length) };
  if (id.startsWith(GROUP_PREFIX)) {
    const teamId = id.slice(GROUP_PREFIX.length);
    return { kind: "group", teamId: teamId === NO_TEAM ? null : teamId };
  }
  return null;
}

interface DragState {
  graph: MoveGraph;
  activeAgentId: string | null;
  onMove: (agentId: string, target: MoveTarget) => void;
}

const DragContext = createContext<DragState | null>(null);

function useDragState(): DragState {
  const value = useContext(DragContext);
  if (!value) throw new Error("Org drag pieces must render inside OrgDnd");
  return value;
}

/**
 * The one draggable per agent row lives on the row wrapper (so the *whole
 * card* is the grab target for a pointer, not a hard-to-hit 6px handle), and
 * its keyboard activator lives on the leading grip button (so a keyboard user
 * still has one focusable "pick me up" control that doesn't swallow the card's
 * click-to-open or its Move… menu). This context is how the row hands the grip
 * the pieces it needs without a second `useDraggable`.
 */
interface RowDrag {
  attributes: DraggableAttributes;
  listeners: DraggableSyntheticListeners;
  setActivatorNodeRef: (node: HTMLElement | null) => void;
}

const RowDragContext = createContext<RowDrag | null>(null);

/**
 * Agent rows sit inside team sections, and team sections nest, so several
 * droppables can be under the pointer at once. An agent row always wins (it
 * is the more specific intent); otherwise the smallest group rectangle wins,
 * which is the innermost team.
 */
const collisionDetection: CollisionDetection = (args) => {
  const hits = pointerWithin(args);
  const candidates = hits.length > 0 ? hits : rectIntersection(args);
  if (candidates.length === 0) return candidates;
  const agentHit = candidates.find((hit) => String(hit.id).startsWith(AGENT_PREFIX));
  if (agentHit) return [agentHit];
  const area = (id: string | number) => {
    const rect = args.droppableRects.get(id);
    return rect ? rect.width * rect.height : Number.POSITIVE_INFINITY;
  };
  return [[...candidates].sort((a, b) => area(a.id) - area(b.id))[0]];
};

/** Human-readable name of a drop target, used in announcements. */
function targetLabel(target: MoveTarget, graph: MoveGraph): string {
  if (target.kind === "agent") {
    const agent = graph.agents.find((candidate) => candidate.id === target.agentId);
    return agent ? `reporting to ${agent.name}` : "an agent";
  }
  if (target.teamId === null) return "Independent, with no team and no manager";
  const team = graph.teams.find((candidate) => candidate.id === target.teamId);
  return `the top level of ${team?.name ?? "a team"}`;
}

export function OrgDnd({
  graph,
  onMove,
  children,
}: {
  graph: MoveGraph;
  onMove: (agentId: string, target: MoveTarget) => void;
  children: (api: OrgMoveApi) => React.ReactNode;
}) {
  const [activeAgentId, setActiveAgentId] = useState<string | null>(null);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor),
  );

  const state = useMemo<DragState>(
    () => ({ graph, activeAgentId, onMove }),
    [graph, activeAgentId, onMove],
  );

  const activeAgent = graph.agents.find((agent) => agent.id === activeAgentId) ?? null;

  const describe = (overId: string | number | undefined, agentId: string | undefined) => {
    if (!agentId || overId === undefined) return null;
    const target = targetFromDroppableId(String(overId));
    if (!target) return null;
    const check = canMove(agentId, target, graph);
    if (!check.ok) return `Can’t drop here. ${check.reason}`;
    if (!resolveMove(agentId, target, graph)) return `Already ${targetLabel(target, graph)}.`;
    return `Drop to move to ${targetLabel(target, graph)}.`;
  };

  const announcements: Announcements = {
    onDragStart: ({ active }) => {
      const name = graph.agents.find((agent) => agent.id === active.data.current?.agentId)?.name;
      return `Picked up ${name ?? "agent"}. Use the arrow keys to choose a team or a manager, space to drop, escape to cancel.`;
    },
    onDragOver: ({ active, over }) =>
      describe(over?.id, active.data.current?.agentId as string | undefined) ??
      "No drop target here.",
    onDragEnd: ({ active, over }) => {
      const agentId = active.data.current?.agentId as string | undefined;
      if (!over || !agentId) return "Move cancelled. Nothing changed.";
      const target = targetFromDroppableId(String(over.id));
      if (!target) return "Move cancelled. Nothing changed.";
      const check = canMove(agentId, target, graph);
      if (!check.ok) return `Move refused. ${check.reason}`;
      if (!resolveMove(agentId, target, graph)) return "Nothing changed.";
      return `Dropped. Moving to ${targetLabel(target, graph)}.`;
    },
    onDragCancel: () => "Move cancelled. Nothing changed.",
  };

  const onDragStart = (event: DragStartEvent) => {
    setActiveAgentId((event.active.data.current?.agentId as string | undefined) ?? null);
  };

  const onDragEnd = (event: DragEndEvent) => {
    setActiveAgentId(null);
    const agentId = event.active.data.current?.agentId as string | undefined;
    const overId = event.over?.id;
    if (!agentId || overId === undefined) return;
    const target = targetFromDroppableId(String(overId));
    if (target) onMove(agentId, target);
  };

  const api: OrgMoveApi = {
    groupWrapper: (teamId, teamName, content) => (
      <GroupDropZone key={groupDroppableId(teamId)} teamId={teamId} teamName={teamName}>
        {content}
      </GroupDropZone>
    ),
    agentRow: (agent, row) => <AgentDropRow agent={agent}>{row}</AgentDropRow>,
    agentHandle: (agent) => <DragHandle agent={agent} />,
    agentMenu: (agent) => <MoveMenu agent={agent} graph={graph} onMove={(target) => onMove(agent.id, target)} />,
  };

  return (
    <DragContext.Provider value={state}>
      <DndContext
        sensors={sensors}
        collisionDetection={collisionDetection}
        accessibility={{
          announcements,
          screenReaderInstructions: {
            draggable:
              "Press space to pick this agent up. While dragging, the arrow keys move between teams and agents, space drops, and escape cancels.",
          },
        }}
        measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
        onDragStart={onDragStart}
        onDragEnd={onDragEnd}
        onDragCancel={() => setActiveAgentId(null)}
      >
        {children(api)}
        <DragOverlay dropAnimation={null}>
          {activeAgent ? (
            <div className="pointer-events-none rounded-xl border border-accent bg-surface px-3.5 py-2.5 text-sm font-medium shadow-card">
              {activeAgent.name}
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>
    </DragContext.Provider>
  );
}

/**
 * The visible grip: a hover/focus cue that the card can be dragged, and the
 * single keyboard activator for the row. Pointer dragging is handled by the
 * row wrapper (the whole card), so this button only needs the keyboard
 * listener — giving it `onPointerDown` too would start the drag twice, once
 * here and once via the bubble to the wrapper.
 */
function DragHandle({ agent }: { agent: OrgAgentNode }) {
  const row = useContext(RowDragContext);
  if (!row) return null;
  const { attributes, listeners, setActivatorNodeRef } = row;
  return (
    <button
      ref={setActivatorNodeRef}
      type="button"
      {...attributes}
      onKeyDown={listeners?.onKeyDown as React.KeyboardEventHandler<HTMLButtonElement> | undefined}
      title={`Drag ${agent.name} to another team or manager`}
      style={{ touchAction: "none" }}
      className={`inline-flex h-8 w-6 shrink-0 cursor-grab items-center justify-center rounded-lg text-faint transition-colors hover:bg-hover hover:text-dim ${focusRing}`}
    >
      <GripVertical size={14} aria-hidden />
      <span className="sr-only">Drag {agent.name}</span>
    </button>
  );
}

function AgentDropRow({ agent, children }: { agent: OrgAgentNode; children: React.ReactNode }) {
  const { graph, activeAgentId } = useDragState();
  const dragging = activeAgentId !== null;
  const check = dragging
    ? canMove(activeAgentId, { kind: "agent", agentId: agent.id }, graph)
    : { ok: true as const };
  const { setNodeRef: setDropRef, isOver } = useDroppable({
    id: `${AGENT_PREFIX}${agent.id}`,
    data: { kind: "agent", agentId: agent.id },
    disabled: !dragging,
  });
  // The draggable is the whole card, not the grip. A pointer-down anywhere on
  // the card starts a drag once it clears the 6px activation distance, so a
  // click still opens the agent and the Move… menu still opens — but a real
  // drag no longer depends on hitting a tiny handle.
  const {
    attributes,
    listeners,
    setNodeRef: setDragRef,
    setActivatorNodeRef,
    isDragging: isSelfDragging,
  } = useDraggable({
    id: `drag:${agent.id}`,
    data: { agentId: agent.id },
    attributes: { roleDescription: "org chart card" },
  });

  const rejected = dragging && !check.ok;
  const tone = !dragging
    ? ""
    : rejected
      ? isOver
        ? "rounded-xl outline-2 outline-dashed outline-danger"
        : "opacity-45"
      : isOver
        ? "rounded-xl outline-2 outline-accent"
        : "";

  return (
    <RowDragContext.Provider value={{ attributes, listeners, setActivatorNodeRef }}>
      <div
        ref={setDropRef}
        data-testid={`drop-agent-${agent.id}`}
        data-drop-state={!dragging ? "idle" : rejected ? "invalid" : isOver ? "over" : "valid"}
        aria-disabled={rejected || undefined}
        className={`relative ${tone}`}
      >
        {/* Mouse/pen drag surface: the whole card. Only the pointer activator
            lives here; the keyboard activator is the grip button, which reads
            `listeners.onKeyDown` from context. Touch is deliberately left to
            scroll the map — touch users reorganise from the Outline's Move…
            menu, which is the mobile default. */}
        <div
          ref={setDragRef}
          onPointerDown={listeners?.onPointerDown as React.PointerEventHandler<HTMLDivElement> | undefined}
          className={`rounded-xl transition-opacity ${
            isSelfDragging ? "opacity-40" : ""
          } ${dragging ? "" : "cursor-grab active:cursor-grabbing"}`}
        >
          {children}
        </div>
        {rejected && isOver && check.reason ? (
          <p
            aria-hidden
            className="pointer-events-none absolute -bottom-1 left-2 right-2 translate-y-full rounded-lg bg-danger-soft px-2 py-1 text-[11px] font-medium text-danger shadow-card"
          >
            {check.reason}
          </p>
        ) : null}
      </div>
    </RowDragContext.Provider>
  );
}

function GroupDropZone({
  teamId,
  teamName,
  children,
}: {
  teamId: string | null;
  teamName: string;
  children: React.ReactNode;
}) {
  const { activeAgentId } = useDragState();
  const dragging = activeAgentId !== null;
  const { setNodeRef, isOver } = useDroppable({
    id: groupDroppableId(teamId),
    data: { kind: "group", teamId },
    disabled: !dragging,
  });
  return (
    <div
      ref={setNodeRef}
      data-testid={`drop-group-${teamId ?? NO_TEAM}`}
      data-drop-state={!dragging ? "idle" : isOver ? "over" : "valid"}
      className={`relative rounded-2xl ${isOver ? "outline-2 outline-dashed outline-accent" : ""}`}
    >
      {children}
      {isOver ? (
        <p
          aria-hidden
          className="pointer-events-none absolute inset-x-3 bottom-2 rounded-lg bg-accent-soft px-2 py-1 text-center text-[11px] font-medium text-accent-strong"
        >
          Top level of {teamName} · no manager
        </p>
      ) : null}
    </div>
  );
}

/** The keyboard-only equivalent used by the Outline view: the same Move…
 * menu, no drag affordances. */
export function menuOnlyMoveApi(
  graph: MoveGraph,
  onMove: (agentId: string, target: MoveTarget) => void,
): OrgMoveApi {
  return {
    groupWrapper: (_teamId, _teamName, content) => content,
    agentRow: (_agent, row) => row,
    agentHandle: () => null,
    agentMenu: (agent) => (
      <MoveMenu agent={agent} graph={graph} onMove={(target) => onMove(agent.id, target)} />
    ),
  };
}
