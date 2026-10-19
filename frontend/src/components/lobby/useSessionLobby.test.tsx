// @vitest-environment jsdom
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelEnvelope } from "../../api/sessionChannel";
import { envelope, lobbyFixture, spotlightFixture } from "./fixtures";
import { useSessionLobby, type SessionLobbyState, type UseSessionLobbyOptions } from "./useSessionLobby";

function frame(e: ChannelEnvelope, event = e.type): string {
  return `id: ${e.seq}\nevent: ${event}\ndata: ${JSON.stringify(e)}\n\n`;
}
const closed = (reason: string) => `event: closed\ndata: ${JSON.stringify({ reason })}\n\n`;
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function stream(frames: string[], opts: { hold?: boolean; signal?: AbortSignal } = {}): Response {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(enc.encode(f));
      if (!opts.hold) controller.close();
      opts.signal?.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")));
    },
  });
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

interface Call { url: string; init: RequestInit }
let calls: Call[];
let lobbyBody: unknown;
let eventResponders: Array<(init: RequestInit) => Response>;

function installFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      if (url.endsWith("/lobby")) return json(lobbyBody);
      if (url.endsWith("/spotlight")) return json(spotlightFixture());
      if (url.endsWith("/presence")) return new Response(null, { status: 204 });
      if (url.endsWith("/events")) {
        const next = eventResponders.shift();
        if (!next) return stream([], { hold: true, signal: init.signal ?? undefined });
        return next(init);
      }
      throw new Error(`unexpected request ${url}`);
    }),
  );
}

let host: HTMLDivElement;
let root: Root;
let latest: SessionLobbyState;

function Probe({ options }: { options?: UseSessionLobbyOptions }) {
  const state = useSessionLobby(1, { retryMinMs: 1, retryMaxMs: 2, ...options });
  useEffect(() => {
    latest = state;
  });
  return null;
}

async function mount(options?: UseSessionLobbyOptions) {
  await act(async () => root.render(<Probe options={options} />));
}
async function settle(ms = 30) {
  await act(async () => { await new Promise((r) => setTimeout(r, ms)); });
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  calls = [];
  lobbyBody = lobbyFixture({ user_id: 2, role: "player", character_id: 12, character_name: "Bran" });
  eventResponders = [];
  installFetch();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

const changed = (seq: number, version: number, holder: number, reason = "turn_passed") =>
  envelope(seq, "turn.spotlight_changed", {
    ...spotlightFixture({ version, holder: { character_id: holder, character_name: holder === 11 ? "Ayla" : "Bran", user_id: holder - 10, display_name: holder === 11 ? "Alice" : "Bob", present: true } }),
    reason,
  });

describe("useSessionLobby", () => {
  it("loads the roster and spotlight, then applies channel events once and in order", async () => {
    eventResponders = [
      () => stream([frame(changed(1, 4, 12)), frame(changed(2, 5, 11)), frame(changed(2, 5, 11)), frame(changed(1, 4, 12)), closed("access_revoked")]),
    ];
    await mount();
    await settle();
    expect(latest.loaded).toBe(true);
    expect(latest.lobby?.viewer.role).toBe("player");
    // Duplicate (seq 2 twice) and out-of-order (seq 1 again) records changed nothing.
    expect(latest.spotlight?.version).toBe(5);
    expect(latest.spotlight?.holder?.character_name).toBe("Ayla");
    expect(latest.gate.kind).toBe("waiting");
  });

  it("reconnects with the last event id after a dropped stream and keeps going", async () => {
    eventResponders = [
      () => stream([frame(changed(1, 4, 12)), frame(changed(2, 5, 12))]),
      () => stream([frame(changed(3, 6, 11))], { hold: true }),
    ];
    await mount();
    await settle(60);
    const streams = calls.filter((c) => c.url.endsWith("/events"));
    expect(streams.length).toBeGreaterThanOrEqual(2);
    expect(new Headers(streams[0].init.headers).get("last-event-id")).toBeNull();
    expect(new Headers(streams[1].init.headers).get("last-event-id")).toBe("2");
    expect(latest.spotlight?.version).toBe(6);
    expect(latest.status).toBe("live");
  });

  it("marks access as revoked and stops retrying when the channel closes with access_revoked", async () => {
    eventResponders = [() => stream([closed("access_revoked")])];
    await mount();
    await settle(60);
    expect(latest.revoked).toBe(true);
    expect(latest.status).toBe("closed");
    expect(calls.filter((c) => c.url.endsWith("/events"))).toHaveLength(1);
  });

  it("treats a 404 on the roster as lost access", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (url.endsWith("/events") ? new Response(null, { status: 404 }) : json({ detail: "Session not found" }, 404))));
    await mount();
    await settle();
    expect(latest.revoked).toBe(true);
  });

  it("releases the subscription on unmount and opens nothing afterwards", async () => {
    await mount();
    await settle();
    const signal = calls.find((c) => c.url.endsWith("/events"))!.init.signal as AbortSignal;
    expect(signal.aborted).toBe(false);
    const before = calls.length;
    await act(async () => root.unmount());
    expect(signal.aborted).toBe(true);
    await settle(30);
    expect(calls.length).toBe(before);
    root = createRoot(host);
  });

  it("reports another member's finished turn but marks the viewer's own turn", async () => {
    const seen: Array<{ own: boolean; outcome: string }> = [];
    const other = envelope(1, "turn.finished", { outcome: "committed" }, { user_id: 1, display_name: "Alice", character_id: 11, character_name: "Ayla" });
    const mine = envelope(2, "turn.finished", { outcome: "committed" }, { user_id: 2, display_name: "Bob", character_id: 12, character_name: "Bran" });
    eventResponders = [() => stream([frame(other), frame(mine)], { hold: true })];
    await mount({ onTurnFinished: ({ own, outcome }) => seen.push({ own, outcome }) });
    await settle();
    expect(seen).toEqual([{ own: false, outcome: "committed" }, { own: true, outcome: "committed" }]);
  });

  it("re-reads the roster when a seat changes", async () => {
    eventResponders = [() => stream([frame(changed(1, 4, 12, "seat_joined"))], { hold: true })];
    await mount();
    await settle();
    expect(calls.filter((c) => c.url.endsWith("/lobby")).length).toBe(2);
  });

  it("shows a shared turn in progress as activity until it finishes", async () => {
    const actor = { user_id: 1, display_name: "Alice", character_id: 11, character_name: "Ayla" };
    eventResponders = [() => stream([frame(envelope(1, "turn.action", { text: "x" }, actor)), frame(envelope(2, "turn.stage", { stage: "narrating" }, actor))], { hold: true })];
    await mount();
    await settle();
    expect(latest.activity?.stage).toBe("narrating");
  });
});
