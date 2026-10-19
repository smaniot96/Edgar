import { describe, expect, it } from "vitest";

import type { ChannelEnvelope, SpotlightState } from "./sessionChannel";
import {
  applyPacingEvent,
  canQueue,
  queuedStatusText,
  queuedTurnFields,
  waitingState,
} from "./tablePacing";
import type { QueuedAction } from "./tablePacing";

const member = (user_id: number, name: string, extra = {}) => ({
  character_id: user_id * 10, character_name: name, user_id, display_name: name, present: true, ...extra,
});
const item = (user_id: number, extra: Partial<QueuedAction> = {}): QueuedAction => ({
  id: "a".repeat(32), user_id, display_name: null, character_id: user_id * 10, character_name: null,
  status: "queued", stale_reason: null, version: 1, created_at: "t", updated_at: "t", expires_at: "t", ...extra,
});
const state = (extra: Partial<SpotlightState> = {}): SpotlightState => ({
  session_id: 1, engaged: true, mode: "round_robin", version: 3, timeout_seconds: 300,
  holder: member(1, "Alice"), since: null, handover_due: false, turn_deadline: null,
  next_up: [member(2, "Bob"), member(3, "Eve")],
  pacing: { policy: "standard", label: "Standard", summary: "", timeout_seconds: 300, auto_pass: false, queue_ttl_seconds: 3600 },
  queue: { revision: 1, items: [] },
  ...extra,
});
const envelope = (type: string, data: unknown): ChannelEnvelope => ({
  seq: 1, type, channel: "system", actor: null, turn_id: null, at: "t", data: data as Record<string, unknown>,
});

describe("waitingState", () => {
  it("says whose turn it is and how far away the viewer is", () => {
    const s = state({ turn_deadline: new Date(100_000).toISOString() });
    const bob = waitingState(s, 2, 40_000);
    expect(bob).toMatchObject({ kind: "waiting", holderName: "Alice", turnsAhead: 1, secondsLeft: 60 });
    expect(bob.message).toBe("Waiting for Alice. You are next.");
    expect(waitingState(s, 3, 40_000).message).toContain("2 turns away");
    expect(waitingState(s, 1, 40_000).kind).toBe("your_turn");
    expect(waitingState(s, 2, 500_000).secondsLeft).toBe(0);
  });
  it("covers free, host directed, absent and unengaged tables", () => {
    expect(waitingState(state({ holder: null, mode: "free_for_all" }), 2, 0).kind).toBe("free");
    expect(waitingState(state({ holder: null, mode: "owner_directed" }), 2, 0).kind).toBe("host_directed");
    expect(waitingState(state({ holder: member(1, "Alice", { present: false }) }), 2, 0).holderAbsent).toBe(true);
    expect(waitingState(state({ engaged: false }), 2, 0).kind).toBe("not_engaged");
  });
});

describe("queue rules", () => {
  it("lets a waiting player queue once, and never the holder", () => {
    expect(canQueue(state(), 2)).toBe(true);
    expect(canQueue(state(), 1)).toBe(false);
    expect(canQueue(state({ queue: { revision: 2, items: [item(2)] } }), 2)).toBe(false);
    expect(canQueue(state({ engaged: false }), 2)).toBe(false);
  });
  it("describes status and builds the turn fields from the stored text and version", () => {
    expect(queuedStatusText(item(2))).toBe("Queued");
    expect(queuedStatusText(item(2, { status: "stale", stale_reason: "scene_changed" }))).toBe("Out of date: the scene changed");
    expect(queuedStatusText(item(2, { status: "expired" }))).toBe("Expired");
    expect(queuedTurnFields(item(2, { text: "I open the chest.", version: 4 }))).toEqual({
      message: "I open the chest.", queued_action_id: "a".repeat(32), queued_action_version: 4,
    });
  });
});

describe("applyPacingEvent", () => {
  it("replaces the queue only with an equal or newer revision and marks next_up", () => {
    const start = state({ queue: { revision: 5, items: [] } });
    const older = applyPacingEvent(start, envelope("turn.queue_changed", { revision: 4, items: [item(2)] }));
    expect(older).toBe(start);
    const newer = applyPacingEvent(start, envelope("turn.queue_changed", { revision: 6, items: [item(2)] }))!;
    expect(newer.queue?.revision).toBe(6);
    expect(newer.next_up.map((m) => m.queued)).toEqual([true, false]);
    const expired = applyPacingEvent(newer, envelope("turn.queue_changed", { revision: 7, items: [item(2, { status: "expired" })] }))!;
    expect(expired.next_up.map((m) => m.queued)).toEqual([false, false]);
  });
  it("ignores an older spotlight version and keeps the newer queue", () => {
    const start = state({ version: 5, queue: { revision: 9, items: [] } });
    expect(applyPacingEvent(start, envelope("turn.spotlight_changed", { ...state({ version: 4 }), reason: "x" }))).toBe(start);
    const next = applyPacingEvent(
      start,
      envelope("turn.spotlight_changed", { ...state({ version: 6, queue: { revision: 3, items: [] } }), reason: "x" }),
    )!;
    expect(next.version).toBe(6);
    expect(next.queue?.revision).toBe(9);
  });
  it("ignores unrelated records and a queue event before any state", () => {
    expect(applyPacingEvent(null, envelope("turn.queue_changed", { revision: 1, items: [] }))).toBeNull();
    const s = state();
    expect(applyPacingEvent(s, envelope("presence.joined", {}))).toBe(s);
  });
});
