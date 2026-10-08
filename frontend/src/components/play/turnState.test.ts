import { describe, expect, it } from "vitest";

import type { AdjudicationResult } from "../../api/types";
import { initialTurnState, linesFromHistory, turnReducer, type TurnAction, type TurnState } from "./turnState";

function run(actions: TurnAction[], start: TurnState = initialTurnState): TurnState {
  return actions.reduce(turnReducer, start);
}

const adj: AdjudicationResult = {
  success: true,
  damage: 4,
  mechanical_summary: "hit",
  dice_result: 17,
};

describe("turnReducer", () => {
  it("start → status → tokens → done commits one DM line and clears pending", () => {
    const s = run([
      { type: "start", turnId: "t1", input: "I attack" },
      { type: "status", turnId: "t1", stage: "narrating" },
      { type: "adjudication", turnId: "t1", result: adj },
      { type: "token", turnId: "t1", text: "You " },
      { type: "token", turnId: "t1", text: "hit." },
      { type: "done", turnId: "t1", result: { narration: "ignored", combat_state: null } },
    ]);
    expect(s.pending).toBeNull();
    expect(s.lines).toHaveLength(2);
    expect(s.lines[0]).toMatchObject({ role: "user", content: "I attack", turnId: "t1" });
    expect(s.lines[1]).toMatchObject({ role: "dm", content: "You hit.", adjudication: adj, isTurn: true });
    expect(s.announcement).toBe("You hit.");
  });

  it("falls back to the done payload's narration when no tokens streamed", () => {
    const s = run([
      { type: "start", turnId: "t1", input: "look" },
      { type: "done", turnId: "t1", result: { narration: "A dark room." } },
    ]);
    expect(s.lines[1]).toMatchObject({ role: "dm", content: "A dark room." });
  });

  it("records the combat outcome when combat ended this turn", () => {
    const s = run([
      { type: "start", turnId: "t1", input: "strike" },
      {
        type: "done",
        turnId: "t1",
        result: { narration: "It falls.", combat_state: { round: 2, ended: true, outcome: "victory", combatants: [] } },
      },
    ]);
    expect(s.lines[1]).toMatchObject({ combatOutcome: "victory" });
  });

  it("on failure keeps partial narration as interrupted, marks the user line, adds a retryable error", () => {
    const s = run([
      { type: "start", turnId: "t1", input: "open door" },
      { type: "token", turnId: "t1", text: "The door creaks" },
      { type: "fail", turnId: "t1", reason: "error", message: "Connection lost", code: "incomplete" },
    ]);
    expect(s.pending).toBeNull();
    expect(s.lines.map((l) => l.role)).toEqual(["user", "dm", "error"]);
    expect(s.lines[0]).toMatchObject({ failed: true });
    expect(s.lines[1]).toMatchObject({ content: "The door creaks", interrupted: true });
    expect(s.lines[2]).toMatchObject({ content: "Connection lost", retryInput: "open door", code: "incomplete" });
    expect(s.announcement).toBe("Connection lost");
  });

  it("on failure without partial text adds no empty DM line", () => {
    const s = run([
      { type: "start", turnId: "t1", input: "go" },
      { type: "fail", turnId: "t1", reason: "stopped", message: "Stopped" },
    ]);
    expect(s.lines.map((l) => l.role)).toEqual(["user", "error"]);
  });

  it("campaign-ended failure drops the orphan user line and shows a notice", () => {
    const s = run([
      { type: "start", turnId: "t1", input: "go" },
      { type: "fail", turnId: "t1", reason: "ended", message: "Campaign has ended" },
    ]);
    expect(s.lines).toHaveLength(1);
    expect(s.lines[0]).toMatchObject({ role: "notice" });
  });

  it("clearTurn removes every line of a failed turn (before retry)", () => {
    const base = run([{ type: "reset", lines: linesFromHistory([{ role: "dm", content: "Hi", created_at: "" }]) }]);
    const s = run(
      [
        { type: "start", turnId: "t1", input: "go" },
        { type: "token", turnId: "t1", text: "par" },
        { type: "fail", turnId: "t1", reason: "error", message: "x" },
        { type: "clearTurn", turnId: "t1" },
      ],
      base,
    );
    expect(s.lines).toEqual(base.lines);
  });

  it("ignores events from a stale/unknown turn", () => {
    const s1 = run([{ type: "start", turnId: "t2", input: "go" }]);
    const s2 = run(
      [
        { type: "token", turnId: "t1", text: "stale" },
        { type: "done", turnId: "t1", result: { narration: "stale" } },
      ],
      s1,
    );
    expect(s2).toBe(s1);
  });
});
