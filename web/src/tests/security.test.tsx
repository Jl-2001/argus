import { describe, it, expect, vi } from "vitest";
import { screen } from "@testing-library/react";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, extname } from "node:path";
import { renderWithProviders } from "@/tests/testUtils";
import { OverviewPage } from "@/pages/OverviewPage";
import { ApplicationDetailPage } from "@/pages/ApplicationDetailPage";
import { IncidentDetailPage } from "@/pages/IncidentDetailPage";
import { SystemPage } from "@/pages/SystemPage";
import * as systemApi from "@/api/system";
import * as applicationsApi from "@/api/applications";
import * as incidentsApi from "@/api/incidents";
import * as evidenceApi from "@/api/evidence";
import {
  fakeApplicationDetail, fakeBundle, fakeDoctorHealthy, fakeExplanationsList, fakeIncidentDetail,
  fakeSystemStatusHealthy,
} from "@/tests/fixtures";

vi.mock("@/api/system");
vi.mock("@/api/applications");
vi.mock("@/api/incidents");
vi.mock("@/api/evidence");

// Fake-but-plausible secrets -- the same shape a real leak would take,
// never a real credential.
const FAKE_SECRETS = [
  "sk-ant-FAKE-SECRET-DO-NOT-LEAK-1234567890",
  "AIzaFAKE-GEMINI-SECRET-DO-NOT-LEAK",
  "super-secret-db-password",
];

describe("no known fake secrets render anywhere on the dashboard", () => {
  it("Overview", async () => {
    vi.mocked(systemApi.getSystemStatus).mockResolvedValue(fakeSystemStatusHealthy);
    vi.mocked(incidentsApi.listIncidents).mockResolvedValue({ incidents: [] });
    vi.mocked(applicationsApi.getApplicationHistory).mockResolvedValue({ application: "x", since: null, transitions: [] });

    const { container } = renderWithProviders(<OverviewPage />);
    await screen.findByText("CNSTRCT");
    assertNoSecrets(container.textContent ?? "");
  });

  it("Application detail", async () => {
    vi.mocked(applicationsApi.getApplication).mockResolvedValue(fakeApplicationDetail);
    vi.mocked(applicationsApi.getApplicationHistory).mockResolvedValue({ application: "x", since: null, transitions: [] });

    const { container } = renderWithProviders(<ApplicationDetailPage />, {
      route: "/applications/cnstrct", path: "/applications/:key",
    });
    await screen.findByRole("heading", { name: "CNSTRCT" });
    assertNoSecrets(container.textContent ?? "");
  });

  it("Incident detail (evidence + AI explanation)", async () => {
    vi.mocked(incidentsApi.getIncident).mockResolvedValue(fakeIncidentDetail);
    vi.mocked(evidenceApi.getIncidentBundle).mockResolvedValue(fakeBundle);
    vi.mocked(evidenceApi.getIncidentExplanations).mockResolvedValue(fakeExplanationsList);

    const { container } = renderWithProviders(<IncidentDetailPage />, {
      route: "/incidents/14", path: "/incidents/:id",
    });
    await screen.findByText("db_connection_timeout");
    assertNoSecrets(container.textContent ?? "");
  });

  it("System", async () => {
    vi.mocked(systemApi.getDoctor).mockResolvedValue(fakeDoctorHealthy);
    vi.mocked(systemApi.getSystemStatus).mockResolvedValue(fakeSystemStatusHealthy);

    const { container } = renderWithProviders(<SystemPage />);
    await screen.findByText("Configuration");
    assertNoSecrets(container.textContent ?? "");
  });
});

describe("no fake secret is hardcoded anywhere in the app's own source", () => {
  it("scans every src/ file for known fake-secret shapes", () => {
    const srcDir = join(__dirname, "..");
    const selfPath = join(__dirname, "security.test.tsx"); // this file legitimately defines FAKE_SECRETS
    const offenders: string[] = [];
    for (const file of listFiles(srcDir)) {
      if (![".ts", ".tsx"].includes(extname(file)) || file === selfPath) continue;
      const text = readFileSync(file, "utf-8");
      for (const secret of FAKE_SECRETS) {
        if (text.includes(secret)) offenders.push(`${file.replace(srcDir, "src")}: ${secret}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

function assertNoSecrets(text: string) {
  for (const secret of FAKE_SECRETS) {
    expect(text).not.toContain(secret);
  }
  expect(text).not.toMatch(/DATABASE_URL|API_KEY=|Bearer [A-Za-z0-9._-]{10,}/);
}

function listFiles(dir: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) files.push(...listFiles(full));
    else files.push(full);
  }
  return files;
}
