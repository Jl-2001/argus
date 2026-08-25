import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { screen, waitFor, render } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/tests/testUtils";
import { MockEventSource } from "@/tests/realtime/mockEventSource";
import { RealtimeProvider } from "@/realtime/RealtimeProvider";
import { TopBar } from "./TopBar";
import * as systemApi from "@/api/system";
import { fakeSystemStatusHealthy } from "@/tests/fixtures";

vi.mock("@/api/system");

function renderTopBar() {
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <RealtimeProvider>
        <TopBar onOpenMenu={() => {}} />
      </RealtimeProvider>
    </QueryClientProvider>,
  );
}

describe("TopBar realtime indicator", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
    vi.mocked(systemApi.getSystemStatus).mockResolvedValue(fakeSystemStatusHealthy);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows Live once the SSE connection opens, distinct from collector status", async () => {
    renderTopBar();
    expect(screen.getByText("Connecting…")).toBeInTheDocument();

    MockEventSource.instances[0]!.simulateOpen();
    await waitFor(() => expect(screen.getByText("Live")).toBeInTheDocument());

    // The collector's own status ("Monitoring") is a separate badge --
    // both are visible at once, never merged into one signal.
    expect(await screen.findByText("Monitoring")).toBeInTheDocument();
  });

  it("shows a reconnecting indicator without disturbing the rest of the page", async () => {
    renderTopBar();
    MockEventSource.instances[0]!.simulateError(MockEventSource.CONNECTING);
    await waitFor(() => expect(screen.getByText("Reconnecting…")).toBeInTheDocument());
  });

  it("shows a polling-fallback message when SSE gives up entirely", async () => {
    renderTopBar();
    MockEventSource.instances[0]!.simulateError(MockEventSource.CLOSED);
    await waitFor(() => expect(screen.getByText("Realtime disconnected — polling")).toBeInTheDocument());
  });
});
