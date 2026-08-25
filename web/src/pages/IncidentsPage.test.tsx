import { describe, it, expect, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/tests/testUtils";
import { IncidentsPage } from "./IncidentsPage";
import * as incidentsApi from "@/api/incidents";
import { fakeIncidentsListOpen, fakeIncidentsListResolved } from "@/tests/fixtures";

vi.mock("@/api/incidents");

describe("IncidentsPage", () => {
  it("defaults to the Open filter and shows open incidents", async () => {
    vi.mocked(incidentsApi.listIncidents).mockResolvedValue(fakeIncidentsListOpen);

    renderWithProviders(<IncidentsPage />);

    await screen.findByText("#14");
    expect(incidentsApi.listIncidents).toHaveBeenCalledWith("open");
    expect(screen.getByText("Open", { selector: "span" })).toBeInTheDocument();
  });

  it("switching to All requests every incident, including resolved ones", async () => {
    const user = userEvent.setup();
    vi.mocked(incidentsApi.listIncidents).mockResolvedValue(fakeIncidentsListOpen);

    renderWithProviders(<IncidentsPage />);
    await screen.findByText("#14");

    vi.mocked(incidentsApi.listIncidents).mockResolvedValue(fakeIncidentsListResolved);
    await user.click(screen.getByRole("tab", { name: "All" }));

    await screen.findByText("#12");
    expect(incidentsApi.listIncidents).toHaveBeenLastCalledWith("all");
    expect(screen.getByText("Resolved", { selector: "span" })).toBeInTheDocument();
  });

  it("shows an empty state, not an error, when there are no open incidents", async () => {
    vi.mocked(incidentsApi.listIncidents).mockResolvedValue({ incidents: [] });

    renderWithProviders(<IncidentsPage />);

    expect(await screen.findByText("No open incidents.")).toBeInTheDocument();
  });
});
