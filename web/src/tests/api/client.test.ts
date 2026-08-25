import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { apiGet, ApiError, ApiUnreachableError } from "@/api/client";
import { ARGUS_API_URL } from "@/lib/env";

describe("apiGet", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("requests the correct URL, GET only", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));

    await apiGet("/api/v1/system/status");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${ARGUS_API_URL}/api/v1/system/status`);
    expect(init.method).toBe("GET");
  });

  it("appends query params, omitting undefined values", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify([]), { status: 200 }));

    await apiGet("/api/v1/applications", { status: "UNHEALTHY", limit: undefined });

    const [url] = fetchMock.mock.calls[0] as [string];
    const parsed = new URL(url);
    expect(parsed.searchParams.get("status")).toBe("UNHEALTHY");
    expect(parsed.searchParams.has("limit")).toBe(false);
  });

  it("parses a successful JSON response", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ hello: "argus" }), { status: 200 }));

    const result = await apiGet<{ hello: string }>("/api/v1/system/status");
    expect(result).toEqual({ hello: "argus" });
  });

  it("parses a `null` JSON body (e.g. GET .../explanations/latest with none cached)", async () => {
    fetchMock.mockResolvedValue(new Response("null", { status: 200 }));

    const result = await apiGet("/api/v1/incidents/1/explanations/latest");
    expect(result).toBeNull();
  });

  it("throws ApiError with the backend's own code/message on a 404", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "incident_not_found", message: "Incident #999 was not found." } }), {
        status: 404,
      }),
    );

    await expect(apiGet("/api/v1/incidents/999")).rejects.toMatchObject({
      status: 404, code: "incident_not_found", message: "Incident #999 was not found.",
    });
  });

  it("ApiError is an instance of ApiError specifically", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "database_unavailable", message: "oops" } }), { status: 503 }),
    );

    await expect(apiGet("/api/v1/system/status")).rejects.toBeInstanceOf(ApiError);
  });

  it("falls back to a generic message for a malformed (non-envelope) error body", async () => {
    fetchMock.mockResolvedValue(new Response("not json at all", { status: 500 }));

    await expect(apiGet("/api/v1/system/status")).rejects.toMatchObject({
      status: 500, code: "unknown_error",
    });
  });

  it("falls back to a generic message when the error body is JSON but not the {error:{}} envelope", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: "some other shape" }), { status: 422 }));

    await expect(apiGet("/api/v1/system/status")).rejects.toMatchObject({ status: 422, code: "unknown_error" });
  });

  it("throws ApiUnreachableError when the network request itself fails", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(apiGet("/api/v1/system/status")).rejects.toBeInstanceOf(ApiUnreachableError);
  });

  it("never confuses ApiUnreachableError with ApiError", async () => {
    fetchMock.mockRejectedValue(new TypeError("network down"));
    try {
      await apiGet("/api/v1/system/status");
      expect.unreachable();
    } catch (error) {
      expect(error).not.toBeInstanceOf(ApiError);
      expect(error).toBeInstanceOf(ApiUnreachableError);
    }
  });
});
