import { describe, expect, it } from "vitest";

import type { ChatMessageRead, TurnTraceRecord } from "../../api/types";
import { attachTraces, initialTurnState, linesFromHistory, turnReducer, type DmLine, type Line } from "./turnState";

function dm(turnId: string | null, createdAt: string, content = "The door opens."): ChatMessageRead {
  return {
    role: "dm",
    content,
    turn_id: turnId,
    created_at: createdAt,
    citations: [],
    ruling_citation_ids: [],
  };
}

function record(turnId: string, createdAt: string): TurnTraceRecord {
  return {
    turn_id: turnId,
    created_at: createdAt,
    turn_diagnostics: null,
    combat_round: null,
    trace: {
      version: 1,
      scope: "dm_turn",
      turn_id: turnId,
      delivery: "sync",
      path: "normal",
      parsed_input: null,
      rag: {
        rules: { count: 0, chunks: [], chunks_dropped: 0 },
        adventure: { count: 0, chunks: [], chunks_dropped: 0 },
      },
      settings: {
        rules_edition: "2014",
        source_mode: "module",
        narrator_profile: "play.narrator",
        profile_version: 1,
        profiles: {},
      },
      composed_with: [],
    },
  };
}

function dmLine(lines: Line[], id: string): DmLine {
  const line = lines.find((l) => l.id === id);
  if (!line || line.role !== "dm") throw new Error(`no DM line ${id}`);
  return line;
}

describe("reloaded DM turns carry a stored-trace state", () => {
  const history = [
    { role: "user", content: "I search.", turn_id: null, created_at: "2026-10-17T10:00:00Z" } as ChatMessageRead,
    dm("turn-a", "2026-10-17T10:00:01Z"),
    dm(null, "2026-10-17T10:00:02Z", "Legacy narration."),
  ];

  it("starts stored turns as loading and turns without a turn_id as not captured", () => {
    const lines = linesFromHistory(history);
    expect(dmLine(lines, "h1")).toMatchObject({ storedTurnId: "turn-a", trace: { status: "loading" } });
    expect(dmLine(lines, "h2")).toMatchObject({ trace: { status: "not_captured" } });
    expect(dmLine(lines, "h2").storedTurnId).toBeUndefined();
  });

  it("captured turns get their record; absent turns are not captured", () => {
    const lines = attachTraces(linesFromHistory(history), {
      status: "loaded",
      records: [record("turn-a", "2026-10-17T10:00:01Z")],
      truncated: false,
    });
    const captured = dmLine(lines, "h1").trace;
    expect(captured).toMatchObject({ status: "captured" });
    expect(captured && captured.status === "captured" && captured.record.turn_id).toBe("turn-a");
  });

  it("reports an older turn outside a truncated window as such, never as not captured", () => {
    const older = dm("turn-old", "2026-10-16T09:00:00Z");
    const lines = attachTraces(linesFromHistory([older, dm("turn-b", "2026-10-17T10:00:01Z")]), {
      status: "loaded",
      records: [record("turn-b", "2026-10-17T10:00:01Z")],
      truncated: true,
    });
    expect(dmLine(lines, "h0").trace).toEqual({ status: "older_than_window" });
    expect(dmLine(lines, "h1").trace).toMatchObject({ status: "captured" });
  });

  it("turns a missing trace endpoint into disabled and a failed read into failed", () => {
    const base = linesFromHistory(history);
    expect(dmLine(attachTraces(base, { status: "disabled" }), "h1").trace).toEqual({ status: "disabled" });
    expect(dmLine(attachTraces(base, { status: "failed" }), "h1").trace).toEqual({ status: "failed" });
    expect(dmLine(attachTraces(base, { status: "loading" }), "h1").trace).toEqual({ status: "loading" });
  });

  it("leaves live turns alone: their developer state is the live debug frame", () => {
    const live: Line = { id: "live", role: "dm", content: "Live.", turnId: "turn-live", isTurn: true };
    const lines = attachTraces([live], { status: "loaded", records: [], truncated: false });
    expect(lines[0]).toBe(live);
    expect(dmLine(lines, "live").trace).toBeUndefined();
  });

  it("the traces action attaches the lookup through the reducer", () => {
    const state = turnReducer(initialTurnState, { type: "reset", lines: linesFromHistory(history) });
    const next = turnReducer(state, {
      type: "traces",
      lookup: { status: "loaded", records: [record("turn-a", "2026-10-17T10:00:01Z")], truncated: false },
    });
    expect(dmLine(next.lines, "h1").trace).toMatchObject({ status: "captured" });
    expect(next.pending).toBeNull();
  });
});
