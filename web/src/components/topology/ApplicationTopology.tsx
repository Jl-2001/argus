import { useMemo } from "react";
import ReactFlow, { Background, type Edge, type Node, Position } from "reactflow";
import "reactflow/dist/style.css";
import type { ApplicationDetailResponse } from "@/api/types";
import { HealthBadge } from "@/components/status/HealthBadge";

const NODE_WIDTH = 168;
const COLUMN_GAP = 32;
const ROW_HEIGHT = 110;

interface TopologyNodeData {
  title: string;
  subtitle?: string;
  status?: string;
}

function TopologyNode({ data }: { data: TopologyNodeData }) {
  return (
    <div className="w-42 rounded-lg border border-border bg-card px-3 py-2 text-card-foreground shadow-sm">
      <p className="truncate text-xs font-semibold">{data.title}</p>
      {data.subtitle && <p className="truncate text-[11px] text-muted-foreground">{data.subtitle}</p>}
      {data.status && (
        <div className="mt-1">
          <HealthBadge status={data.status} />
        </div>
      )}
    </div>
  );
}

const nodeTypes = { argus: TopologyNode };

/**
 * The first, deliberately literal Argus topology view: `Application`
 * owns `Service`(s), `Service` owns (at most one, in v0.1) `Container`.
 * These are the *only* two relationships rendered -- every edge here
 * corresponds to a real foreign key already in Argus's schema
 * (`services.application_id`, `containers.service_id`). This is NOT
 * dependency inference: it never draws e.g. `API -> PostgreSQL`, since
 * Argus does not (yet) know that relationship actually exists. See the
 * milestone's own "Application Topology" section.
 */
export function ApplicationTopology({ detail }: { detail: ApplicationDetailResponse }) {
  const { nodes, edges } = useMemo(() => buildTopology(detail), [detail]);

  return (
    <div className="h-80 w-full rounded-lg border border-border" role="img" aria-label={`Topology: application ${detail.name} owns its services, each owning at most one container`}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnScroll
        zoomOnScroll={false}
      >
        <Background gap={16} />
      </ReactFlow>
    </div>
  );
}

export function buildTopology(detail: ApplicationDetailResponse): { nodes: Node[]; edges: Edge[] } {
  const services = detail.services;
  const rowWidth = Math.max(services.length, 1) * (NODE_WIDTH + COLUMN_GAP);

  const nodes: Node[] = [
    {
      id: "app",
      type: "argus",
      data: { title: detail.name, subtitle: "Application", status: detail.status } satisfies TopologyNodeData,
      position: { x: rowWidth / 2 - NODE_WIDTH / 2, y: 0 },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
    },
  ];
  const edges: Edge[] = [];

  services.forEach((service, index) => {
    const serviceId = `service-${index}`;
    const x = index * (NODE_WIDTH + COLUMN_GAP);

    nodes.push({
      id: serviceId,
      type: "argus",
      data: {
        title: service.compose_service ?? service.name,
        subtitle: "Service",
        status: service.status,
      } satisfies TopologyNodeData,
      position: { x, y: ROW_HEIGHT },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
    });
    edges.push({ id: `app-${serviceId}`, source: "app", target: serviceId });

    if (service.container) {
      const containerId = `container-${index}`;
      nodes.push({
        id: containerId,
        type: "argus",
        data: {
          title: service.container.name,
          subtitle: `${service.container.docker_state}${service.container.docker_health ? ` · ${service.container.docker_health}` : ""}`,
        } satisfies TopologyNodeData,
        position: { x, y: ROW_HEIGHT * 2 },
        targetPosition: Position.Top,
      });
      edges.push({ id: `${serviceId}-${containerId}`, source: serviceId, target: containerId });
    }
  });

  return { nodes, edges };
}
