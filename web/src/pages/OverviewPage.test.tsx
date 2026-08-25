import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithProviders } from "@/tests/testUtils";
import { OverviewPage } from "./OverviewPage";
import { ApiOfflineGate } from "@/components/layout/ApiOfflineGate";
import { ApiUnreachableError } from "@/api/client";
import * as systemApi from "@/api/system";
import * as applicationsApi from "@/api/applications";
import * as incidentsApi from "@/api/incidents";
import { fakeSystemStatusHealthy, fakeSystemStatusWithIncident, fakeIncidentsListOpen } from "@/tests/fixtures";

vi.mock("@/api/system");
vi.mock("@/api/applications");
vi.mock("@/api/incidents");

describe("OverviewPage", () => {
  beforeEach(() => {
    vi.mocked(applicationsApi.getApplicationHistory).mockResolvedValue({
      application: "cnstrct", since: null, transitions: [],
    });
  });

  it("renders a healthy state with no open incidents", async () => {
    vi.mocked(systemApi.getSystemStatus).mockResolvedValue(fakeSystemStatusHealthy);
    vi.mocked(incidentsApi.listIncidents).mockResolvedValue({ incidents: [] });

    renderWithProviders(<OverviewPage />);

    expect(await screen.findByText("Monitoring healthy")).toBeInTheDocument();
    expect(screen.getByText("CNSTRCT")).toBeInTheDocument();
    expect(await screen.findByText("No open incidents.")).toBeInTheDocument();
  });

  it("prominently shows an active incident", async () => {
    vi.mocked(systemApi.getSystemStatus).mockResolvedValue(fakeSystemStatusWithIncident);
    vi.mocked(incidentsApi.listIncidents).mockResolvedValue(fakeIncidentsListOpen);

    renderWithProviders(<OverviewPage />);

    expect(await screen.findByText(/1 active incident/)).toBeInTheDocument();
    expect(await screen.findByText("#14")).toBeInTheDocument();
    expect(screen.getByText("View Incident")).toBeInTheDocument();
  });

  it("never triggers AI generation or shows a generate button", async () => {
    vi.mocked(systemApi.getSystemStatus).mockResolvedValue(fakeSystemStatusWithIncident);
    vi.mocked(incidentsApi.listIncidents).mockResolvedValue(fakeIncidentsListOpen);

    renderWithProviders(<OverviewPage />);
    await screen.findByText("#14");

    expect(screen.queryByText(/generate/i)).not.toBeInTheDocument();
  });
});

describe("ApiOfflineGate", () => {
  beforeEach(() => {
    vi.mocked(applicationsApi.getApplicationHistory).mockResolvedValue({
      application: "cnstrct", since: null, transitions: [],
    });
  });

  it("shows ARGUS API OFFLINE (not an empty/healthy dashboard) when the API is unreachable", async () => {
    vi.mocked(systemApi.getSystemStatus).mockRejectedValue(new ApiUnreachableError(new Error("network down")));

    renderWithProviders(
      <ApiOfflineGate>
        <OverviewPage />
      </ApiOfflineGate>,
    );

    expect(await screen.findByText("ARGUS API OFFLINE")).toBeInTheDocument();
    // The underlying page must never render through -- a failed fetch
    // is never interpreted as an empty/healthy state.
    expect(screen.queryByText("Monitoring healthy")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("renders children normally once the API is reachable", async () => {
    vi.mocked(systemApi.getSystemStatus).mockResolvedValue(fakeSystemStatusHealthy);
    vi.mocked(incidentsApi.listIncidents).mockResolvedValue({ incidents: [] });

    renderWithProviders(
      <ApiOfflineGate>
        <OverviewPage />
      </ApiOfflineGate>,
    );

    await waitFor(() => expect(screen.getByText("CNSTRCT")).toBeInTheDocument());
    expect(screen.queryByText("ARGUS API OFFLINE")).not.toBeInTheDocument();
  });
});
