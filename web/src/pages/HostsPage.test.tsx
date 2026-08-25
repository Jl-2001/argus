import { describe, it, expect } from "vitest";
import { screen, within } from "@testing-library/react";
import { renderWithProviders } from "@/tests/testUtils";
import { HostsPage } from "./HostsPage";
import * as hostsApi from "@/api/hosts";
import { fakeHostLocal, fakeHostRemote, fakeHostRemoteOffline } from "@/tests/fixtures";
import { vi } from "vitest";

vi.mock("@/api/hosts");

function table() {
  return within(screen.getByRole("table"));
}

describe("HostsPage", () => {
  it("renders every host with its status", async () => {
    vi.mocked(hostsApi.listHosts).mockResolvedValue([fakeHostLocal, fakeHostRemote]);

    renderWithProviders(<HostsPage />);

    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(table().getByText("Local Host")).toBeInTheDocument();
    expect(table().getByText("Ubuntu Dell")).toBeInTheDocument();
    expect(table().getAllByText("Online")).toHaveLength(2);
  });

  it("shows an offline remote host distinctly", async () => {
    vi.mocked(hostsApi.listHosts).mockResolvedValue([fakeHostLocal, fakeHostRemoteOffline]);

    renderWithProviders(<HostsPage />);
    await screen.findByRole("table");

    expect(table().getByText("Offline")).toBeInTheDocument();
    expect(table().getByText("Old Server")).toBeInTheDocument();
  });

  it("labels the local host as this machine", async () => {
    vi.mocked(hostsApi.listHosts).mockResolvedValue([fakeHostLocal]);

    renderWithProviders(<HostsPage />);
    await screen.findByRole("table");

    expect(table().getByText("this machine")).toBeInTheDocument();
  });

  it("shows an empty state, not an error, when there are no hosts", async () => {
    vi.mocked(hostsApi.listHosts).mockResolvedValue([]);

    renderWithProviders(<HostsPage />);

    expect(await screen.findByText("No hosts registered yet.")).toBeInTheDocument();
  });

  it("never renders a management button (no self-registration UI)", async () => {
    vi.mocked(hostsApi.listHosts).mockResolvedValue([fakeHostLocal, fakeHostRemote]);

    renderWithProviders(<HostsPage />);
    await screen.findByRole("table");

    expect(screen.queryByRole("button", { name: /add|register|remove|delete/i })).not.toBeInTheDocument();
  });
});
