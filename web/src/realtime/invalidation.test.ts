import { describe, it, expect, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { invalidateEverything, invalidateForEvent } from "./invalidation";

function spiedClient() {
  const client = new QueryClient();
  const spy = vi.spyOn(client, "invalidateQueries");
  return { client, spy };
}

function keysInvalidated(spy: ReturnType<typeof spiedClient>["spy"]): unknown[][] {
  return spy.mock.calls.map((call: unknown[]) => (call[0] as { queryKey: unknown[] }).queryKey);
}

describe("invalidateForEvent", () => {
  it("collector.tick invalidates system status and hosts", () => {
    const { client, spy } = spiedClient();
    invalidateForEvent(client, "collector.tick");
    const keys = keysInvalidated(spy);
    expect(keys).toContainEqual(["system", "status"]);
    expect(keys).toContainEqual(["hosts"]);
  });

  it.each(["application.status_changed", "service.status_changed", "container.status_changed"] as const)(
    "%s invalidates applications, system status, and hosts",
    (type) => {
      const { client, spy } = spiedClient();
      invalidateForEvent(client, type);
      const keys = keysInvalidated(spy);
      expect(keys).toContainEqual(["applications"]);
      expect(keys).toContainEqual(["system", "status"]);
      expect(keys).toContainEqual(["hosts"]);
    },
  );

  it.each(["incident.opened", "incident.updated", "incident.resolved"] as const)(
    "%s invalidates incidents, applications, and system status",
    (type) => {
      const { client, spy } = spiedClient();
      invalidateForEvent(client, type);
      const keys = keysInvalidated(spy);
      expect(keys).toContainEqual(["incidents"]);
      expect(keys).toContainEqual(["applications"]);
      expect(keys).toContainEqual(["system", "status"]);
    },
  );

  it("evidence.updated invalidates incidents (covers evidence + bundle for any mounted incident)", () => {
    const { client, spy } = spiedClient();
    invalidateForEvent(client, "evidence.updated");
    expect(keysInvalidated(spy)).toEqual([["incidents"]]);
  });

  it("evidence.health_changed invalidates system (status + doctor)", () => {
    const { client, spy } = spiedClient();
    invalidateForEvent(client, "evidence.health_changed");
    expect(keysInvalidated(spy)).toEqual([["system"]]);
  });

  it("explanation.available invalidates incidents", () => {
    const { client, spy } = spiedClient();
    invalidateForEvent(client, "explanation.available");
    expect(keysInvalidated(spy)).toEqual([["incidents"]]);
  });
});

describe("invalidateEverything", () => {
  it("invalidates system, applications, incidents, and hosts", () => {
    const { client, spy } = spiedClient();
    invalidateEverything(client);
    const keys = keysInvalidated(spy);
    expect(keys).toContainEqual(["system"]);
    expect(keys).toContainEqual(["applications"]);
    expect(keys).toContainEqual(["incidents"]);
    expect(keys).toContainEqual(["hosts"]);
  });
});
