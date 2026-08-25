import { ARGUS_API_URL } from "@/lib/env";
import type { ApiErrorBody } from "./types";

/**
 * The one function every `src/api/*.ts` resource module calls through.
 * Deliberately GET-only -- there is no `post`/`put`/`patch`/`delete`
 * export anywhere in this module or this directory, matching
 * Milestone 14's read-only scope (see
 * `src/tests/api/readOnlyGuard.test.ts` for the automated proof that
 * no other file in `src/api/` ever calls `fetch` with a different
 * method).
 */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/** Thrown when the request never reached the API at all (network
 * failure, DNS, connection refused) -- distinct from `ApiError`, which
 * means the API *did* respond, just with an error status. The
 * distinction matters to the UI: this one means "Argus API offline",
 * that one means e.g. "incident not found". */
export class ApiUnreachableError extends Error {
  constructor(cause: unknown) {
    super("Could not reach the Argus API.");
    this.name = "ApiUnreachableError";
    this.cause = cause;
  }
}

function buildUrl(path: string, params?: Record<string, string | number | undefined>): string {
  const url = new URL(`${ARGUS_API_URL}${path.startsWith("/") ? path : `/${path}`}`);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/** GET `path` (optionally with query `params`) and parse the JSON body
 * as `T`. Never sends anything but a GET request. Raises `ApiError` for
 * a non-2xx response (using the backend's `{"error": {code, message}}`
 * envelope when present), or `ApiUnreachableError` if the request
 * couldn't be sent/completed at all. */
export async function apiGet<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const url = buildUrl(path, params);

  let response: Response;
  try {
    response = await fetch(url, { method: "GET", headers: { Accept: "application/json" } });
  } catch (cause) {
    throw new ApiUnreachableError(cause);
  }

  if (!response.ok) {
    const body = await safeParseErrorBody(response);
    throw new ApiError(
      response.status,
      body?.error.code ?? "unknown_error",
      body?.error.message ?? `Request failed with status ${response.status}`,
    );
  }

  // A 200 with an empty body (e.g. "no content") is never something
  // this API actually returns -- every endpoint returns JSON, `null`
  // included (see GET .../explanations/latest) -- so parsing as JSON
  // unconditionally is correct here, not a special case.
  return (await response.json()) as T;
}

async function safeParseErrorBody(response: Response): Promise<ApiErrorBody | null> {
  try {
    const parsed: unknown = await response.json();
    if (
      typeof parsed === "object" && parsed !== null && "error" in parsed &&
      typeof (parsed as { error?: unknown }).error === "object"
    ) {
      return parsed as ApiErrorBody;
    }
    return null;
  } catch {
    return null;
  }
}
