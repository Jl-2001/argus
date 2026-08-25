import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { screen, render, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/tests/testUtils";
import { MockEventSource } from "@/tests/realtime/mockEventSource";
import { useArgusEvents } from "./useArgusEvents";
import { ARGUS_API_URL } from "@/lib/env";

function StateProbe() {
  const state = useArgusEvents();
  return <div data-testid="state">{state}</div>;
}

function renderProbe() {
  const queryClient = createTestQueryClient();
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <StateProbe />
    </QueryClientProvider>,
  );
  return { ...utils, queryClient, invalidateSpy };
}

describe("useArgusEvents", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("connects to the correct URL", () => {
    renderProbe();
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0]!.url).toBe(`${ARGUS_API_URL}/api/v1/events`);
  });

  it("only ever opens one connection for the whole probe tree", () => {
    renderProbe();
    expect(MockEventSource.instances).toHaveLength(1);
  });

  it("starts connecting, then becomes live on open", async () => {
    renderProbe();
    expect(screen.getByTestId("state").textContent).toBe("connecting");

    MockEventSource.instances[0]!.simulateOpen();
    await waitFor(() => expect(screen.getByTestId("state").textContent).toBe("live"));
  });

  it("becomes reconnecting on a transient error (readyState still CONNECTING)", async () => {
    renderProbe();
    const source = MockEventSource.instances[0]!;
    source.simulateOpen();
    await waitFor(() => expect(screen.getByTestId("state").textContent).toBe("live"));

    source.simulateError(MockEventSource.CONNECTING);
    await waitFor(() => expect(screen.getByTestId("state").textContent).toBe("reconnecting"));
  });

  it("becomes offline when the browser gives up (readyState CLOSED) -- polling still works elsewhere", async () => {
    renderProbe();
    const source = MockEventSource.instances[0]!;
    source.simulateError(MockEventSource.CLOSED);
    await waitFor(() => expect(screen.getByTestId("state").textContent).toBe("offline"));
  });

  it("invalidates the mapped query on a recognized event", async () => {
    const { invalidateSpy } = renderProbe();
    const source = MockEventSource.instances[0]!;
    source.simulateOpen();

    source.dispatch("incident.opened", JSON.stringify({ schema_version: 1, incident_id: 14, application_key: "musipal", opening_status: "DEGRADED" }));

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalled());
    const keys = invalidateSpy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey);
    expect(keys).toContainEqual(["incidents"]);
  });

  it("invalidates everything on stream.reset", async () => {
    const { invalidateSpy } = renderProbe();
    const source = MockEventSource.instances[0]!;
    source.dispatch("stream.reset", JSON.stringify({ schema_version: 1, reason: "history_unavailable" }));

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalled());
    const keys = invalidateSpy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey);
    expect(keys).toContainEqual(["system"]);
    expect(keys).toContainEqual(["applications"]);
    expect(keys).toContainEqual(["incidents"]);
  });

  it("malformed event data is ignored, never crashes, and never invalidates", async () => {
    const { invalidateSpy } = renderProbe();
    const source = MockEventSource.instances[0]!;

    expect(() => source.dispatch("incident.opened", "{not valid json")).not.toThrow();
    // give any (incorrect) async invalidation a chance to happen before asserting it didn't
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(invalidateSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId("state")).toBeInTheDocument(); // UI still standing
  });

  it("an event type the app never registered for is simply never delivered", () => {
    renderProbe();
    const source = MockEventSource.instances[0]!;
    // Nothing is listening for this type -- MockEventSource itself
    // guarantees no handler fires, mirroring real EventSource, which
    // only ever calls listeners registered via addEventListener for
    // that exact event name.
    expect(() => source.dispatch("totally.unrecognized.event", "{}")).not.toThrow();
  });

  it("closes the EventSource on unmount -- no leaked connection", () => {
    const { unmount } = renderProbe();
    const source = MockEventSource.instances[0]!;
    expect(source.closed).toBe(false);

    unmount();

    expect(source.closed).toBe(true);
  });
});
