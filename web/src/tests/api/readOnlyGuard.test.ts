import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, extname } from "node:path";

/**
 * Milestone 14's own read-only guarantee, enforced as an actual test
 * rather than just a lint rule (see eslint.config.js's matching
 * `no-restricted-globals` rule, which is only an edit-time nudge):
 *
 * 1. `fetch()` is called in exactly one file, `src/api/client.ts`.
 * 2. Nothing in `src/` ever passes a non-GET `method` to `fetch`.
 * 3. Every function exported from `src/api/*.ts` (the typed client
 *    layer) calls through `apiGet`, never `fetch` directly.
 */

const SRC_DIR = join(__dirname, "..", "..");

function listSourceFiles(dir: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      files.push(...listSourceFiles(full));
    } else if ([".ts", ".tsx"].includes(extname(full))) {
      files.push(full);
    }
  }
  return files;
}

describe("frontend read-only guarantee", () => {
  const sourceFiles = listSourceFiles(SRC_DIR);

  // `src/tests/**` is test scaffolding (mocks/fixtures/this file's own
  // prose), not application code -- excluded from both scans below so
  // this guard checks what actually ships, not how it's tested.
  const applicationFiles = sourceFiles.filter((file) => !file.includes(join("src", "tests") + "/"));

  it("fetch() is called in exactly one application file: src/api/client.ts", () => {
    const callers = applicationFiles.filter((file) => /\bfetch\s*\(/.test(readFileSync(file, "utf-8")));
    expect(callers.map((f) => f.replace(SRC_DIR, "src"))).toEqual([join("src", "api", "client.ts")]);
  });

  it("no application file requests a non-GET HTTP method anywhere", () => {
    const mutatingMethodPattern = /method\s*:\s*["'`](POST|PUT|PATCH|DELETE)["'`]/i;
    const offenders = applicationFiles.filter((file) => mutatingMethodPattern.test(readFileSync(file, "utf-8")));
    expect(offenders).toEqual([]);
  });

  it("client.ts itself only ever issues a GET request", () => {
    const text = readFileSync(join(SRC_DIR, "api", "client.ts"), "utf-8");
    expect(text).toMatch(/method:\s*"GET"/);
    expect(text).not.toMatch(/method:\s*["'`](POST|PUT|PATCH|DELETE)["'`]/i);
  });

  it("every src/api/*.ts resource module calls apiGet, not fetch, for its requests", () => {
    const apiDir = join(SRC_DIR, "api");
    const resourceFiles = readdirSync(apiDir).filter(
      (f) => f.endsWith(".ts") && !["client.ts", "types.ts"].includes(f),
    );
    expect(resourceFiles.length).toBeGreaterThan(0);
    for (const file of resourceFiles) {
      const text = readFileSync(join(apiDir, file), "utf-8");
      expect(text).toMatch(/apiGet</);
      expect(text).not.toMatch(/\bfetch\s*\(/);
    }
  });
});
