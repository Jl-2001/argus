import { describe, it, expect, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { renderWithProviders } from "@/tests/testUtils";
import { ApplicationDetailPage } from "./ApplicationDetailPage";
import * as applicationsApi from "@/api/applications";
import { fakeApplicationDetail } from "@/tests/fixtures";
import { ApiError } from "@/api/client";

vi.mock("@/api/applications");

function renderPage() {
  return renderWithProviders(<ApplicationDetailPage />, { route: "/applications/cnstrct", path: "/applications/:key" });
}

describe("ApplicationDetailPage", () => {
  it("renders service and container rows with their real fields", async () => {
    vi.mocked(applicationsApi.getApplication).mockResolvedValue(fakeApplicationDetail);
    vi.mocked(applicationsApi.getApplicationHistory).mockResolvedValue({
      application: "cnstrct", since: null, transitions: [],
    });

    renderPage();

    expect(await screen.findByRole("heading", { name: "CNSTRCT" })).toBeInTheDocument();
    // "cnstrct-api-1" also appears as a React Flow topology node label --
    // scope to the Services table specifically.
    const servicesTable = within(screen.getByRole("table"));
    expect(servicesTable.getByText("cnstrct-api-1")).toBeInTheDocument();
    expect(servicesTable.getByText("cnstrct-postgres-1")).toBeInTheDocument();
    expect(servicesTable.getAllByText("running")).toHaveLength(2); // both api and postgres containers
    expect(servicesTable.getByText("3000/tcp → 0.0.0.0:3000")).toBeInTheDocument();
  });

  it("shows which host the application is on", async () => {
    vi.mocked(applicationsApi.getApplication).mockResolvedValue(fakeApplicationDetail);
    vi.mocked(applicationsApi.getApplicationHistory).mockResolvedValue({
      application: "cnstrct", since: null, transitions: [],
    });

    renderPage();

    expect(await screen.findByText(/Host: Local Host/)).toBeInTheDocument();
  });

  it("never renders secrets/labels/env for a container", async () => {
    vi.mocked(applicationsApi.getApplication).mockResolvedValue(fakeApplicationDetail);
    vi.mocked(applicationsApi.getApplicationHistory).mockResolvedValue({
      application: "cnstrct", since: null, transitions: [],
    });

    const { container } = renderPage();
    await screen.findByRole("heading", { name: "CNSTRCT" });

    expect(container.textContent).not.toMatch(/DATABASE_URL|SECRET|PASSWORD/i);
  });

  it("shows a clean 'not found' error, not a stack trace, for an unknown application", async () => {
    vi.mocked(applicationsApi.getApplication).mockRejectedValue(
      new ApiError(404, "application_not_found", "Application 'cnstrct' was not found."),
    );

    renderPage();

    expect(await screen.findByText("Application 'cnstrct' was not found.")).toBeInTheDocument();
  });
});
