import { describe, it, expect, vi } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/tests/testUtils";
import { SystemPage } from "./SystemPage";
import * as systemApi from "@/api/system";
import { fakeDoctorHealthy, fakeDoctorFailing, fakeSystemStatusHealthy } from "@/tests/fixtures";

vi.mock("@/api/system");

describe("SystemPage", () => {
  it("renders every doctor check with a PASS badge when healthy", async () => {
    vi.mocked(systemApi.getDoctor).mockResolvedValue(fakeDoctorHealthy);
    vi.mocked(systemApi.getSystemStatus).mockResolvedValue(fakeSystemStatusHealthy);

    renderWithProviders(<SystemPage />);

    expect(await screen.findByText("Configuration")).toBeInTheDocument();
    expect(screen.getByText("Docker connection")).toBeInTheDocument();
    expect(screen.getAllByText("Pass")).toHaveLength(7);
    expect(screen.getByText("Argus is operational.")).toBeInTheDocument();
  });

  it("surfaces failing checks with their message and marks the system not operational", async () => {
    vi.mocked(systemApi.getDoctor).mockResolvedValue(fakeDoctorFailing);
    vi.mocked(systemApi.getSystemStatus).mockResolvedValue(fakeSystemStatusHealthy);

    renderWithProviders(<SystemPage />);

    expect(await screen.findByText("database file does not exist at /fake/path/argus.db")).toBeInTheDocument();
    expect(screen.getByText("could not reach the Docker daemon")).toBeInTheDocument();
    expect(screen.getAllByText("Fail")).toHaveLength(2);
    expect(screen.getAllByText("Skip")).toHaveLength(3);
    expect(screen.getByText("Argus is not fully operational.")).toBeInTheDocument();
  });

  it("distinguishes Argus's own health from application health", async () => {
    vi.mocked(systemApi.getDoctor).mockResolvedValue(fakeDoctorHealthy);
    vi.mocked(systemApi.getSystemStatus).mockResolvedValue(fakeSystemStatusHealthy);

    renderWithProviders(<SystemPage />);

    expect(await screen.findByText(/Argus's own operational health/)).toBeInTheDocument();
  });
});
