import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearApiToken,
  getApiToken,
  hasApiToken,
  setApiToken,
  withAuthorization,
} from "./auth";
import { ApiError, apiFetch, fetchBlob } from "./client";
import { safeTurnError } from "./turnStream";

// Fake test token only. It is never a real credential and exists only inside this file.
const TEST_TOKEN = "edgar_test_fixture_token_0123456789";

/** A Map-backed Storage so the tests can see exactly what would be persisted. */
function fakeStorage(): Storage & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    get length() { return data.size; },
    clear: () => data.clear(),
    getItem: (key: string) => data.get(key) ?? null,
    key: (index: number) => [...data.keys()][index] ?? null,
    removeItem: (key: string) => { data.delete(key); },
    setItem: (key: string, value: string) => { data.set(key, String(value)); },
  };
}

let session: ReturnType<typeof fakeStorage>;
let touchedLocal: boolean;

beforeEach(() => {
  session = fakeStorage();
  touchedLocal = false;
  vi.stubGlobal("sessionStorage", session);
  // Any read or write of localStorage would be a persistence bug (EDG-160 acceptance).
  vi.stubGlobal("localStorage", new Proxy({}, {
    get() { touchedLocal = true; throw new Error("localStorage must not be used for the token"); },
    set() { touchedLocal = true; throw new Error("localStorage must not be used for the token"); },
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("token storage", () => {
  it("keeps the token in sessionStorage only", () => {
    expect(setApiToken(`  ${TEST_TOKEN}  `)).toBe(true);
    expect(getApiToken()).toBe(TEST_TOKEN);
    expect(hasApiToken()).toBe(true);
    expect([...session.data.values()]).toEqual([TEST_TOKEN]);
    expect(touchedLocal).toBe(false);
  });

  it("refuses malformed values without storing them", () => {
    for (const bad of ["", "   ", "has space", "tab\tinside", "line\nbreak", "x".repeat(513), "été"]) {
      expect(setApiToken(bad)).toBe(false);
    }
    expect(session.data.size).toBe(0);
    expect(hasApiToken()).toBe(false);
  });

  it("clears the token when asked and when storage is blocked", () => {
    setApiToken(TEST_TOKEN);
    clearApiToken();
    expect(getApiToken()).toBeNull();

    vi.stubGlobal("sessionStorage", undefined);
    expect(getApiToken()).toBeNull();
    expect(setApiToken(TEST_TOKEN)).toBe(false);
    expect(() => clearApiToken()).not.toThrow();
  });

  it("treats a throwing storage accessor as no token", () => {
    vi.stubGlobal("sessionStorage", new Proxy({}, { get() { throw new DOMException("blocked", "SecurityError"); } }));
    expect(getApiToken()).toBeNull();
    expect(setApiToken(TEST_TOKEN)).toBe(false);
  });
});

describe("header injection", () => {
  it("leaves the init untouched when no token is set (local mode)", () => {
    const init: RequestInit = { method: "POST", headers: { accept: "text/event-stream" } };
    expect(withAuthorization(init)).toBe(init);
    expect(withAuthorization(undefined)).toBeUndefined();
  });

  it("adds Authorization when a token is set and keeps other headers", () => {
    setApiToken(TEST_TOKEN);
    const init = withAuthorization({ method: "POST", headers: { accept: "text/event-stream" } });
    const headers = new Headers(init?.headers);
    expect(headers.get("authorization")).toBe(`Bearer ${TEST_TOKEN}`);
    expect(headers.get("accept")).toBe("text/event-stream");
  });

  it("sends the header on every client path: JSON, multipart upload and binary resource", async () => {
    setApiToken(TEST_TOKEN);
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/api/sessions");
    await apiFetch("/api/adventures/upload", { method: "POST", body: new FormData() });
    await fetchBlob("/api/sessions/1/map.svg?v=x");

    expect(fetchMock).toHaveBeenCalledTimes(3);
    for (const [, init] of fetchMock.mock.calls as [string, RequestInit][]) {
      expect(new Headers(init.headers).get("authorization")).toBe(`Bearer ${TEST_TOKEN}`);
    }
  });

  it("sends no Authorization header on the client path when no token is set", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    await apiFetch("/api/sessions");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit | undefined];
    expect(init?.headers).toBeUndefined();
  });

  it("never puts the token in the URL", async () => {
    setApiToken(TEST_TOKEN);
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    await apiFetch("/api/sessions/1/turn/stream");
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).not.toContain(TEST_TOKEN);
  });
});

describe("refusals", () => {
  it("maps 401 to a sign-in state that never contains the token", async () => {
    setApiToken(TEST_TOKEN);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ code: "authentication_required", detail: "Authentication required." }),
      { status: 401, headers: { "www-authenticate": "Bearer" } },
    )));
    const err = await apiFetch("/api/sessions").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(401);
    expect(apiErr.failure.category).toBe("authentication");
    expect(apiErr.message).toMatch(/access token is missing, revoked or not recognized/);
    expect(apiErr.message).toMatch(/Settings/);
    expect(JSON.stringify(apiErr)).not.toContain(TEST_TOKEN);
    expect(apiErr.message).not.toContain(TEST_TOKEN);
  });

  it("maps an unstructured 401 (no safe code in the body) to the same sign-in state", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 401 })));
    const err = await apiFetch("/api/sessions").catch((e: unknown) => e) as ApiError;
    expect(err.failure.code).toBe("authentication_required");
    expect(err.failure.category).toBe("authentication");
  });

  it("maps 403 to the administrator-only state", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{\"detail\":\"Admin access required.\"}", { status: 403 })));
    const err = await apiFetch("/api/adventures/upload", { method: "POST" }).catch((e: unknown) => e) as ApiError;
    expect(err.failure.code).toBe("forbidden");
    expect(err.failure.category).toBe("forbidden");
    expect(err.message).toMatch(/administrator account/);
  });

  it("gives the turn stream the same sign-in copy from a bare safe code", () => {
    const failure = safeTurnError({ code: "authentication_required" });
    expect(failure.category).toBe("authentication");
    expect(failure.code).toBe("authentication_required");
  });
});
