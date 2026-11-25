"use client";

import "@xyflow/react/dist/base.css";

import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { memo, useMemo } from "react";

import { layoutDag } from "@/lib/dag-layout";
import { statusMeta, StatusIcon } from "./status";

// A type alias (not an interface) so it satisfies React Flow's Record<string, unknown> data bound.
export type DagNodeInput = {
  key: string;
  task_type: string;
  depends_on: string[];
  status?: string; // absent for a definition preview
  attempt?: number;
  max_attempts?: number;
};

type DagNodeData = DagNodeInput & {
  selected: boolean;
  onSelect?: (key: string) => void;
};

type DagNode = Node<DagNodeData, "task">;

const TaskNode = memo(function TaskNode({ data }: NodeProps<DagNode>) {
  const meta = data.status ? statusMeta(data.status) : null;
  const tone = meta?.tone ?? "pending";
  const attempts = data.attempt && data.attempt > 1 ? ` · attempt ${data.attempt}/${data.max_attempts}` : "";
  return (
    <>
      <Handle type="target" position={Position.Left} className="!size-2 !border-0 !bg-transparent" />
      <button
        type="button"
        onClick={() => data.onSelect?.(data.key)}
        aria-pressed={data.onSelect ? data.selected : undefined}
        // No aria-label: the accessible name is the visible text (WCAG 2.5.3 label in name).
        className={`flex w-[200px] items-center gap-2.5 rounded-xl border bg-surface px-3 py-2.5 text-left shadow-card transition-[box-shadow,border-color] ${
          data.selected ? "ring-2 ring-[var(--focus)]" : ""
        } ${data.onSelect ? "cursor-pointer hover:border-border-strong" : "cursor-default"}`}
        style={{
          borderColor: data.status ? `var(--st-${tone})` : "var(--border)",
          borderLeftWidth: data.status ? 4 : 1,
        }}
      >
        {data.status ? <StatusIcon status={data.status} className="size-5" /> : null}
        <span className="min-w-0 flex-1">
          <span className="block truncate font-mono text-sm font-medium">{data.key}</span>
          <span className="block truncate text-xs text-muted">
            {data.task_type}
            {meta ? ` · ${meta.label}` : ""}
            {attempts}
          </span>
        </span>
      </button>
      <Handle type="source" position={Position.Right} className="!size-2 !border-0 !bg-transparent" />
    </>
  );
});

const nodeTypes = { task: TaskNode };
const enableNodePointerEvents = () => {};

export function DagView({
  nodes,
  selectedKey,
  onSelect,
  height = 340,
  label,
}: {
  nodes: DagNodeInput[];
  selectedKey?: string | null;
  onSelect?: (key: string) => void;
  height?: number;
  label: string;
}) {
  const { flowNodes, flowEdges } = useMemo(() => {
    const positions = new Map(layoutDag(nodes).map((p) => [p.key, p]));
    const statusOf = new Map(nodes.map((n) => [n.key, n.status]));
    const flowNodes: DagNode[] = nodes.map((n) => ({
      id: n.key,
      type: "task",
      position: { x: positions.get(n.key)!.x, y: positions.get(n.key)!.y },
      data: { ...n, selected: n.key === selectedKey, onSelect },
      draggable: false,
      connectable: false,
      ariaRole: "group",
    }));
    const flowEdges: Edge[] = nodes.flatMap((n) =>
      n.depends_on.map((dep) => {
        const upstream = statusOf.get(dep);
        const flowing = upstream === "succeeded" && statusOf.get(n.key) === "running";
        return {
          id: `${dep}->${n.key}`,
          source: dep,
          target: n.key,
          animated: flowing,
          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
          style: upstream === "succeeded" ? { stroke: "var(--st-succeeded)", strokeWidth: 1.5 } : undefined,
          focusable: false,
        };
      }),
    );
    return { flowNodes, flowEdges };
  }, [nodes, selectedKey, onSelect]);

  return (
    <figure className="card overflow-hidden" style={{ height }} aria-label={label}>
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1.1 }}
        minZoom={0.3}
        maxZoom={1.6}
        nodesDraggable={false}
        nodesConnectable={false}
        nodesFocusable={false}
        edgesFocusable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
        // React Flow only enables pointer events on nodes that have a handler.
        // Selection is handled by the button inside each node (so keyboard and
        // mouse share one code path); this handler just turns clicks on.
        onNodeClick={onSelect ? enableNodePointerEvents : undefined}
        panOnScroll={false}
        zoomOnScroll={false}
      >
        <Background gap={24} size={1} color="var(--border)" />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
      <figcaption className="sr-only">
        {label}.{" "}
        {nodes
          .map(
            (n) =>
              `${n.key}${n.status ? ` is ${statusMeta(n.status).label}` : ""}${n.depends_on.length ? `, after ${n.depends_on.join(" and ")}` : ""}`,
          )
          .join("; ")}
        .
      </figcaption>
    </figure>
  );
}
