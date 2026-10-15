import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  apiFetch,
  assignCharacterToCampaign,
  createSession,
  deleteSession,
  getSession,
  listNpcs,
  uploadAdventure,
} from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("API transport", () => {
  it.each([
    new Headers({ authorization: "Bearer test-token" }),
    [["authorization", "Bearer test-token"]] as [string, string][],
    { authorization: "Bearer test-token" },
  ])("preserves supported Fetch header formats", async (headers) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    const ctrl = new AbortController();
    await apiFetch("/api/sessions", { headers, signal: ctrl.signal });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer test-token");
    expect(init.signal).toBe(ctrl.signal);
  });

  it("keeps abort errors available to callers", async () => {
    const abort = new DOMException("Cancelled", "AbortError");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(abort));
    await expect(apiFetch("/api/sessions")).rejects.toBe(abort);
  });

  it("wraps network failures in a player-safe offline error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));
    await expect(apiFetch("/api/sessions")).rejects.toMatchObject({
      status: 0,
      isOffline: true,
      message: "Can't reach the Edgar server. Is it running?",
    });
  });

  it("retains request IDs and raw errors without exposing internal details in the message", async () => {
    const body = '{"detail":"internal diagnostic"}';
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, {
      status: 500,
      headers: { "x-request-id": "request-123" },
    })));
    const err = await apiFetch("/api/sessions").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({
      status: 500, body, requestId: "request-123",
      message: "Something went wrong on the server. Please try again.",
    });
  });
});

describe("shared request helpers", () => {
  it("requests people for the selected save and reads the tracking state", async () => {
    const npc = {
      id: 7,
      campaign_id: 3,
      session_id: 9,
      name: "Mara",
      disposition: "wary",
      stat_block: {},
      journal_entry_id: "entry_person_9",
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([npc]), { headers: { "x-npc-tracking": "available" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;

    await expect(listNpcs(3, 9, signal)).resolves.toEqual({
      npcs: [npc],
      tracking: "available",
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/campaigns/3/npcs?session_id=9", { signal });
  });

  it("serializes JSON writes and passes cancellation to reads", async () => {
    const fetchMock = vi.fn().mockImplementation(() => new Response('{"id":7}'));
    vi.stubGlobal("fetch", fetchMock);
    await expect(createSession(3, 5)).resolves.toEqual({ id: 7 });
    const [, write] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(write.method).toBe("POST");
    expect(new Headers(write.headers).get("content-type")).toBe("application/json");
    expect(JSON.parse(write.body as string)).toEqual({ campaign_id: 3, active_character_id: 5 });

    const ctrl = new AbortController();
    await getSession(7, ctrl.signal);
    expect(fetchMock.mock.calls[1][1].signal).toBe(ctrl.signal);
  });

  it("does not attempt to parse empty mutation responses", async () => {
    const fetchMock = vi.fn().mockImplementation(() => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(deleteSession(7)).resolves.toBeUndefined();
    await expect(assignCharacterToCampaign(3, 5)).resolves.toBeUndefined();
    const [, write] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(write.body as string)).toEqual({ character_id: 5 });
  });

  it("lets Fetch set the multipart boundary when uploading PDFs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"slug":"adventure"}'));
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["PDF content"], "adventure.pdf", { type: "application/pdf" });
    await uploadAdventure(file, " Adventure ");
    const [, write] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(write.body).toBeInstanceOf(FormData);
    expect((write.body as FormData).get("title")).toBe("Adventure");
    expect(new Headers(write.headers).has("content-type")).toBe(false);
  });
});
