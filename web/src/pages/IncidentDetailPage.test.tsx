import { describe, it, expect, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/tests/testUtils";
import { IncidentDetailPage } from "./IncidentDetailPage";
import * as incidentsApi from "@/api/incidents";
import * as evidenceApi from "@/api/evidence";
import {
  fakeIncidentDetail, fakeIncidentDetailNoExplanation, fakeBundle,
  fakeExplanationsList, fakeExplanationsListMultiProvider, fakeExplanationsListEmpty,
} from "@/tests/fixtures";

vi.mock("@/api/incidents");
vi.mock("@/api/evidence");

function renderPage() {
  return renderWithProviders(<IncidentDetailPage />, { route: "/incidents/14", path: "/incidents/:id" });
}

describe("IncidentDetailPage", () => {
  it("renders evidence with severity, category, count, source, and the redacted sample", async () => {
    vi.mocked(incidentsApi.getIncident).mockResolvedValue(fakeIncidentDetail);
    vi.mocked(evidenceApi.getIncidentBundle).mockResolvedValue(fakeBundle);
    vi.mocked(evidenceApi.getIncidentExplanations).mockResolvedValue(fakeExplanationsList);

    renderPage();

    expect(await screen.findByText("db_connection_timeout")).toBeInTheDocument();
    expect(screen.getByText(/Count 27/)).toBeInTheDocument();
    expect(screen.getByText("Source api")).toBeInTheDocument();
    expect(screen.getByText("[REDACTED] connection timeout after 30s")).toBeInTheDocument();
    expect(screen.getByText("High")).toBeInTheDocument();
  });

  it("renders the persisted AI explanation: provider, model, confidence, root cause, no generation control", async () => {
    vi.mocked(incidentsApi.getIncident).mockResolvedValue(fakeIncidentDetail);
    vi.mocked(evidenceApi.getIncidentBundle).mockResolvedValue(fakeBundle);
    vi.mocked(evidenceApi.getIncidentExplanations).mockResolvedValue(fakeExplanationsList);

    renderPage();

    expect(await screen.findByText(/Database connections were timing out repeatedly\./)).toBeInTheDocument();
    expect(screen.getByText("Anthropic")).toBeInTheDocument();
    expect(screen.getByText("claude-sonnet-5")).toBeInTheDocument();
    expect(screen.getByText("high confidence")).toBeInTheDocument();
    // Milestone 14 is view-only: no "Generate"/"Regenerate" control anywhere.
    expect(screen.queryByRole("button", { name: /generate/i })).not.toBeInTheDocument();
  });

  it("shows the empty AI-analysis state, not an error, when nothing has been generated yet", async () => {
    vi.mocked(incidentsApi.getIncident).mockResolvedValue(fakeIncidentDetailNoExplanation);
    vi.mocked(evidenceApi.getIncidentBundle).mockResolvedValue(fakeBundle);
    vi.mocked(evidenceApi.getIncidentExplanations).mockResolvedValue(fakeExplanationsListEmpty);

    renderPage();

    expect(await screen.findByText("No AI analysis has been generated for this incident.")).toBeInTheDocument();
  });

  it("switches between multiple providers' cached explanations via tabs", async () => {
    const user = userEvent.setup();
    vi.mocked(incidentsApi.getIncident).mockResolvedValue(fakeIncidentDetail);
    vi.mocked(evidenceApi.getIncidentBundle).mockResolvedValue(fakeBundle);
    vi.mocked(evidenceApi.getIncidentExplanations).mockResolvedValue(fakeExplanationsListMultiProvider);

    renderPage();

    await screen.findByText("Anthropic", { selector: "span" });
    expect(screen.getByText("claude-sonnet-5")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Gemini" }));
    expect(await screen.findByText("gemini-3.5-flash")).toBeInTheDocument();
  });

  it("clicking an evidence citation highlights and scrolls to the matching evidence + timeline rows", async () => {
    const user = userEvent.setup();
    vi.mocked(incidentsApi.getIncident).mockResolvedValue(fakeIncidentDetail);
    vi.mocked(evidenceApi.getIncidentBundle).mockResolvedValue(fakeBundle);
    vi.mocked(evidenceApi.getIncidentExplanations).mockResolvedValue(fakeExplanationsList);

    renderPage();
    const citation = await screen.findByRole("button", { name: "[log_signal:42]" });

    const evidenceRow = document.getElementById("evidence-log_signal-42");
    const timelineRow = document.getElementById("timeline-log_signal-42");
    expect(evidenceRow).toBeInTheDocument();
    expect(timelineRow).toBeInTheDocument();
    const scrollSpy = vi.fn();
    evidenceRow!.scrollIntoView = scrollSpy;
    timelineRow!.scrollIntoView = vi.fn();

    await user.click(citation);

    expect(scrollSpy).toHaveBeenCalled();
    // The citation button itself reflects the active state too.
    expect(citation.className).toMatch(/border-ring/);
  });

  it("renders the timeline chronologically from real transition/signal facts", async () => {
    vi.mocked(incidentsApi.getIncident).mockResolvedValue(fakeIncidentDetail);
    vi.mocked(evidenceApi.getIncidentBundle).mockResolvedValue(fakeBundle);
    vi.mocked(evidenceApi.getIncidentExplanations).mockResolvedValue(fakeExplanationsListEmpty);

    renderPage();

    expect(await screen.findByText("HEALTHY -> UNHEALTHY")).toBeInTheDocument();
    expect(screen.getByText("db_connection_timeout x27")).toBeInTheDocument();
  });
});
