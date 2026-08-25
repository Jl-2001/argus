import { describe, it, expect, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/tests/testUtils";
import { ApplicationsPage } from "./ApplicationsPage";
import * as applicationsApi from "@/api/applications";
import { fakeApplicationSummary, fakeUnhealthyApplicationSummary } from "@/tests/fixtures";
import type { ApplicationSummaryResponse } from "@/api/types";

vi.mock("@/api/applications");

// jsdom doesn't apply the `hidden sm:block` / `sm:hidden` Tailwind
// classes that make the desktop table and mobile cards mutually
// exclusive at runtime -- both render into the DOM regardless of
// viewport, so tests scope to the (always-present) table specifically
// rather than asserting on document-wide text, which would see each
// application twice.
function table() {
  return within(screen.getByRole("table"));
}

describe("ApplicationsPage", () => {
  it("renders every application with its status", async () => {
    vi.mocked(applicationsApi.listApplications).mockResolvedValue([
      fakeApplicationSummary, fakeUnhealthyApplicationSummary,
    ]);

    renderWithProviders(<ApplicationsPage />);

    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(table().getByText("CNSTRCT")).toBeInTheDocument();
    expect(table().getByText("Musipal")).toBeInTheDocument();
    expect(table().getByText("Healthy")).toBeInTheDocument();
    expect(table().getByText("Unhealthy")).toBeInTheDocument();
  });

  it("applies the status filter, requesting only that status from the API", async () => {
    const user = userEvent.setup();
    vi.mocked(applicationsApi.listApplications).mockResolvedValue([fakeApplicationSummary]);

    renderWithProviders(<ApplicationsPage />);
    await screen.findByRole("table");

    await user.click(screen.getByRole("tab", { name: "Healthy" }));

    await vi.waitFor(() => {
      expect(applicationsApi.listApplications).toHaveBeenLastCalledWith("HEALTHY");
    });
  });

  it("links each application to its detail page", async () => {
    vi.mocked(applicationsApi.listApplications).mockResolvedValue([fakeApplicationSummary]);

    renderWithProviders(<ApplicationsPage />);
    await screen.findByRole("table");

    const link = table().getByRole("link", { name: "CNSTRCT" });
    expect(link).toHaveAttribute("href", "/applications/cnstrct");
  });

  it("shows which host each application is on", async () => {
    const remoteApp: ApplicationSummaryResponse = {
      ...fakeApplicationSummary, key: "dell:cnstrct", host_key: "dell", host_name: "Ubuntu Dell",
    };
    vi.mocked(applicationsApi.listApplications).mockResolvedValue([fakeApplicationSummary, remoteApp]);

    renderWithProviders(<ApplicationsPage />);
    await screen.findByRole("table");

    expect(table().getByText("Local Host")).toBeInTheDocument();
    expect(table().getByText("Ubuntu Dell")).toBeInTheDocument();
  });

  it("shows an empty state, not an error, when there are no applications", async () => {
    vi.mocked(applicationsApi.listApplications).mockResolvedValue([]);

    renderWithProviders(<ApplicationsPage />);

    expect(await screen.findByText("No applications discovered yet.")).toBeInTheDocument();
  });
});
