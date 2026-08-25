import { describe, it, expect } from "vitest";
import { buildTopology } from "./ApplicationTopology";
import { fakeApplicationDetail } from "@/tests/fixtures";

describe("buildTopology", () => {
  it("renders exactly one node per application/service/container", () => {
    const { nodes } = buildTopology(fakeApplicationDetail);

    // 1 application + 2 services + 2 containers (fakeApplicationDetail
    // fixture: api and postgres, both with a container)
    expect(nodes).toHaveLength(5);
    expect(nodes.map((n) => n.id)).toEqual(["app", "service-0", "container-0", "service-1", "container-1"]);
  });

  it("only renders Application-owns-Service and Service-owns-Container edges -- never a cross-service edge", () => {
    const { edges } = buildTopology(fakeApplicationDetail);

    // Exactly: app->service-0, service-0->container-0, app->service-1, service-1->container-1
    expect(edges).toHaveLength(4);
    for (const edge of edges) {
      const isAppOwnsService = edge.source === "app" && edge.target.startsWith("service-");
      const isServiceOwnsContainer = edge.source.startsWith("service-") && edge.target.startsWith("container-");
      expect(isAppOwnsService || isServiceOwnsContainer).toBe(true);
    }

    // Never a service-to-service or container-to-container edge -- this
    // is NOT dependency inference (see the milestone's own "Application
    // Topology" section): Argus never claims e.g. "API -> PostgreSQL".
    const serviceToService = edges.filter((e) => e.source.startsWith("service-") && e.target.startsWith("service-"));
    const containerToContainer = edges.filter((e) => e.source.startsWith("container-") && e.target.startsWith("container-"));
    expect(serviceToService).toHaveLength(0);
    expect(containerToContainer).toHaveLength(0);
  });

  it("a service with no container yields no container node/edge for it, not a fabricated one", () => {
    const detailWithoutContainer = {
      ...fakeApplicationDetail,
      services: [{ compose_service: "worker", name: "worker", status: "UNKNOWN", container: null }],
    };

    const { nodes, edges } = buildTopology(detailWithoutContainer);

    expect(nodes.map((n) => n.id)).toEqual(["app", "service-0"]);
    expect(edges).toHaveLength(1);
    expect(edges[0]).toMatchObject({ source: "app", target: "service-0" });
  });

  it("node data carries only real fields from the response, never an invented dependency label", () => {
    const { nodes } = buildTopology(fakeApplicationDetail);
    const appNode = nodes.find((n) => n.id === "app")!;
    expect(appNode.data).toEqual({ title: "CNSTRCT", subtitle: "Application", status: "HEALTHY" });
  });
});
