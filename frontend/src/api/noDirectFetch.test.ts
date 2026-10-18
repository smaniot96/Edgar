/**
 * Static guard for the bearer header (EDG-160). Every request must go through `authorizedFetch`
 * in `auth.ts`, so the Authorization header cannot be forgotten on a new call site.
 *
 * This fails when a source file (not a test) calls `fetch`, `EventSource`, XMLHttpRequest or
 * `sendBeacon` directly, or puts an API URL into an `href` or `src` attribute, since a browser
 * navigation or image load cannot carry the header. Use `apiFetch`, `fetchBlob`,
 * `useAuthorizedObjectUrl` or `downloadAuthorized` instead.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SRC = fileURLToPath(new URL("..", import.meta.url));
// The only module allowed to call the network primitives directly.
const TRANSPORT = "api/auth.ts";

const DIRECT_CALLS: Array<[string, RegExp]> = [
  ["fetch(", /(?<![\w$.])fetch\s*\(/],
  ["new EventSource(", /\bnew\s+EventSource\s*\(/],
  ["XMLHttpRequest", /\bXMLHttpRequest\b/],
  ["sendBeacon(", /\bsendBeacon\s*\(/],
];
// A URL under the API handed to an attribute the browser fetches without our header.
const NAVIGATING_API_URL = /\b(?:href|src)=\{[^}\n]*(?:apiBase|\/api\/)/;

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    if (!/\.(ts|tsx)$/.test(entry.name) || /\.test\.(ts|tsx)$/.test(entry.name)) return [];
    return [path];
  });
}

function findings(): string[] {
  const hits: string[] = [];
  for (const file of sourceFiles(SRC)) {
    const rel = relative(SRC, file);
    const lines = readFileSync(file, "utf8").split("\n");
    lines.forEach((line, index) => {
      const where = `${rel}:${index + 1}`;
      // Comment-only lines describe calls rather than make them.
      if (/^\s*(\/\/|\/\*|\*)/.test(line)) return;
      if (rel !== TRANSPORT) {
        for (const [label, pattern] of DIRECT_CALLS) {
          if (pattern.test(line)) hits.push(`${where} uses ${label}`);
        }
      }
      if (NAVIGATING_API_URL.test(line)) hits.push(`${where} puts an API URL in href/src`);
    });
  }
  return hits;
}

describe("bearer header coverage", () => {
  it("routes every network call through authorizedFetch", () => {
    expect(findings()).toEqual([]);
  });

  it("keeps the transport module as the one place that calls fetch", () => {
    const transport = readFileSync(join(SRC, TRANSPORT), "utf8");
    const calls = transport.match(/(?<![\w$.])fetch\s*\(/g) ?? [];
    expect(calls).toHaveLength(1);
    expect(transport).toContain("withAuthorization(init)");
  });

  it("scans a real source tree (the guard cannot pass by finding nothing)", () => {
    expect(sourceFiles(SRC).length).toBeGreaterThan(50);
  });
});
