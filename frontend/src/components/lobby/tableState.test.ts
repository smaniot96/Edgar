import { describe, expect, it } from "vitest";

import {
  buildRoster,
  initialTableState,
  tableReducer,
  turnGate,
  type TableAction,
  type TableState,
} from "./tableState";
import { envelope, lobbyFixture, spotlightFixture } from "./fixtures";

function run(actions: TableAction[], self: number | null = 1, start: TableState = initialTableState): TableState {
  return actions.reduce((state, action) => tableReducer(state, action, self), start);
}

const changed = (seq: number, version: number, reason: string, holderId: number | null, name = "Bran") =>
  ({
    type: "event" as const,
    envelope: envelope(seq, "turn.spotlight_changed", {
      ...spotlightFixture({
        version,
        holder: holderId === null ? null : { character_id: holderId, character_name: name, user_id: holderId - 10, display_name: name, present: true },
      }),
      reason,
    }),
  });

describe("tableReducer", () => {
  it("applies spotlight changes in order and drops a stale version", () => {
    const state = run([changed(1, 4, "turn_passed", 12), changed(2, 3, "assigned", 11, "Ayla")]);
    expect(state.spotlight?.version).toBe(4);
    expect(state.spotlight?.holder?.character_name).toBe("Bran");
  });

  it("does not let an older REST read replace a newer pushed spotlight", () => {
    const state = run([changed(1, 5, "turn_passed", 12), { type: "spotlight", spotlight: spotlightFixture({ version: 4 }) }]);
    expect(state.spotlight?.version).toBe(5);
  });

  it("strips the reason from the stored spotlight and announces whose turn it is", () => {
    const state = run([changed(1, 4, "turn_passed", 12)]);
    expect(state.spotlight).not.toHaveProperty("reason");
    expect(state.announcement).toBe("It is now Bran's turn, played by Bran. The turn passed on.");
    const mine = run([changed(1, 4, "turn_passed", 11, "Ayla")], 1);
    expect(mine.announcement).toContain("It is your turn.");
  });

  it("marks the roster stale only for seat changes", () => {
    expect(run([changed(1, 4, "seat_joined", 12)]).rosterStale).toBe(1);
    expect(run([changed(1, 4, "turn_passed", 12)]).rosterStale).toBe(0);
  });

  it("replaces presence and the spotlight from a snapshot, never merging", () => {
    const first = run([
      { type: "event", envelope: envelope(1, "presence.joined", { user_id: 9 }, { user_id: 9, display_name: "Zed", character_id: null, character_name: null }) },
    ]);
    const next = run(
      [{ type: "snapshot", envelope: envelope(7, "snapshot", { presence: [{ user_id: 2, display_name: "Bob", character_id: 12, character_name: "Bran", state: "active" }], spotlight_state: spotlightFixture({ version: 8 }) }) }],
      1,
      first,
    );
    expect(Object.keys(next.presence)).toEqual(["2"]);
    expect(next.spotlight?.version).toBe(8);
  });

  it("tracks presence joins, idle and active flips and departures, announcing others only", () => {
    const actor = { user_id: 2, display_name: "Bob", character_id: 12, character_name: "Bran" };
    let state = run([{ type: "event", envelope: envelope(1, "presence.joined", { user_id: 2 }, actor) }]);
    expect(state.presence[2].state).toBe("active");
    expect(state.announcement).toBe("Bob joined the table.");
    state = run([{ type: "event", envelope: envelope(2, "presence.idle", { user_id: 2, state: "idle" }, actor) }], 1, state);
    expect(state.presence[2].state).toBe("idle");
    state = run([{ type: "event", envelope: envelope(3, "presence.left", { user_id: 2 }, actor) }], 1, state);
    expect(state.presence[2]).toBeUndefined();
    expect(state.announcement).toBe("Bob left the table.");
    const self = run([{ type: "event", envelope: envelope(1, "presence.joined", { user_id: 1 }, { ...actor, user_id: 1 }) }], 1);
    expect(self.announcement).toBe("");
  });

  it("follows a turn from action through stages to finished", () => {
    const actor = { user_id: 2, display_name: "Bob", character_id: 12, character_name: "Bran" };
    let state = run([{ type: "event", envelope: envelope(1, "turn.action", { text: "I open the door" }, actor) }]);
    expect(state.activity?.actor?.display_name).toBe("Bob");
    state = run([{ type: "event", envelope: envelope(2, "turn.stage", { stage: "narrating" }, actor) }], 1, state);
    expect(state.activity?.stage).toBe("narrating");
    state = run([{ type: "event", envelope: envelope(3, "turn.finished", { outcome: "committed" }, actor) }], 1, state);
    expect(state.activity).toBeNull();
  });
});

describe("turnGate", () => {
  const viewer = (over: Partial<ReturnType<typeof lobbyFixture>["viewer"]>) => lobbyFixture(over).viewer;

  it("lets a spectator watch but never act, whatever the spotlight says", () => {
    const gate = turnGate(viewer({ role: "spectator", user_id: 3, character_id: null }), spotlightFixture());
    expect(gate).toMatchObject({ kind: "spectating", canAct: false });
  });

  it("tells the holder it is their turn", () => {
    const gate = turnGate(viewer({ role: "player", user_id: 1 }), spotlightFixture());
    expect(gate).toMatchObject({ kind: "acting", canAct: true, title: "Your turn" });
  });

  it("tells everyone else who they are waiting for", () => {
    const gate = turnGate(viewer({ role: "player", user_id: 2, character_id: 12 }), spotlightFixture());
    expect(gate.kind).toBe("waiting");
    expect(gate.canAct).toBe(false);
    expect(gate.title).toBe("Waiting for Alice (Ayla)");
  });

  it("opens the input for a seated player in free-for-all with no holder", () => {
    const open = spotlightFixture({ mode: "free_for_all", holder: null });
    expect(turnGate(viewer({ role: "player", user_id: 2, character_id: 12 }), open)).toMatchObject({ kind: "open", canAct: true });
    expect(turnGate(viewer({ role: "owner", user_id: 1, character_id: null }), open)).toMatchObject({ kind: "waiting", canAct: false });
  });

  it("behaves as before where no arbitration applies", () => {
    expect(turnGate(null, null).canAct).toBe(true);
    expect(turnGate(viewer({ user_id: null }), spotlightFixture()).kind).toBe("free");
    expect(turnGate(viewer({ role: "player", user_id: 2, character_id: 12 }), spotlightFixture({ engaged: false })).canAct).toBe(true);
  });

  it("explains a player without a seat", () => {
    const gate = turnGate(viewer({ role: "player", user_id: 4, character_id: null }), spotlightFixture({ engaged: false }));
    expect(gate).toMatchObject({ kind: "unseated", canAct: false });
  });
});

describe("buildRoster", () => {
  it("merges presence, seat and spotlight position and lists spectators last", () => {
    const rows = buildRoster(
      lobbyFixture({ user_id: 2 }),
      { 2: { user_id: 2, display_name: "Bob", character_id: 12, character_name: "Bran", state: "idle" } },
      spotlightFixture({ holder: { character_id: 11, character_name: "Ayla", user_id: 1, display_name: "Alice", present: false } }),
    );
    expect(rows.map((r) => r.name)).toEqual(["Alice", "Bob", "Carol"]);
    expect(rows[0]).toMatchObject({ isHolder: true, present: false });
    expect(rows[1]).toMatchObject({ isYou: true, present: true, idle: true, nextUp: 1 });
    expect(rows[2]).toMatchObject({ role: "spectator", characterName: null });
  });
});
