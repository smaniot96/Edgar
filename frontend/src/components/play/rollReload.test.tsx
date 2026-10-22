// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessageRead, RollRecord } from "../../api/types";
import { MessageList } from "../MessageList";
import { adjudicationFromRoll, rollParts, rollSummary } from "./rollText";
import { linesFromHistory, type Line } from "./turnState";

vi.mock("../../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));

// A stored advantage check: both faces kept on record, the engine's own totals.
const stored: RollRecord = {
  turn_id: "turn-9",
  check: "stealth",
  success: true,
  dc: 15,
  against: "DC",
  modifier: 3,
  roll_expression: "1d20+3",
  natural_roll: 14,
  dice_result: 17,
  damage: null,
  healing: null,
  conditions: [],
  mechanical_summary: "Stealth: 1d20+3 [14, 7] = 17 vs DC 15.",
  dice_log: [
    { expression: "1d20", rolls: [14, 7], modifier: 0, total: 14, purpose: "stealth", roll_mode: "advantage", kept: 14 },
  ],
};

function dm(extra: Partial<ChatMessageRead>): ChatMessageRead {
  return { role: "dm", content: "You slip past.", created_at: "", ...extra };
}

describe("reloaded roll cards", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("rebuilds the live card from the stored record without touching the dice", () => {
    const [line] = linesFromHistory([dm({ turn_id: "turn-9", roll_status: "captured", roll: stored })]);
    expect(line).toMatchObject({ role: "dm", rollStatus: "captured" });
    if (line.role !== "dm") throw new Error("expected a DM line");
    expect(line.adjudication).toEqual(adjudicationFromRoll(stored));
    // Same summary the live turn would have shown for these numbers.
    const parts = rollParts(line.adjudication);
    expect(parts && rollSummary(parts)).toBe("stealth · 1d20+3 [14, 7] = 17 vs DC 15 → Success");
  });

  it("marks legacy turns as not captured and never invents a ruling", () => {
    const [line] = linesFromHistory([dm({})]);
    expect(line).toMatchObject({ role: "dm", rollStatus: "not_captured", adjudication: null });
  });

  it("renders the stored card on reload and the not-captured note for legacy turns", async () => {
    const lines: Line[] = linesFromHistory([
      dm({ content: "Captured tale.", turn_id: "turn-9", roll_status: "captured", roll: stored }),
      dm({ content: "Legacy tale." }),
    ]);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root: Root = createRoot(container);
    await act(() =>
      root.render(<MessageList messages={lines} pending={null} />),
    );
    const cards = container.querySelectorAll('[aria-label^="stealth"]');
    expect(cards).toHaveLength(1);
    expect(cards[0].textContent).toContain("17");
    const notes = container.querySelectorAll('[data-roll-status="not_captured"]');
    expect(notes).toHaveLength(1);
    expect(notes[0].textContent).toContain("not captured");
    await act(() => root.unmount());
    container.remove();
  });

  it("renders reloaded mixed attacks individually and preserves healing before them", async () => {
    const mixed: RollRecord = { ...stored, check: "attack", against: "AC", dc: 14,
      dice_result: 9, modifier: 6, roll_expression: "1d20+6", natural_roll: 3,
      success: false, damage: 7, healing: 10,
      dice_log: [
        { expression: "1d10+4", rolls: [6], modifier: 4, total: 10, purpose: "second wind" },
        { expression: "1d20+6", rolls: [3], modifier: 6, total: 9, purpose: "attack check" },
        { expression: "1d20+6", rolls: [8], modifier: 6, total: 14, purpose: "attack check" },
        { expression: "1d8+4", rolls: [3], modifier: 4, total: 7, purpose: "player damage" },
      ],
    };
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(() => root.render(<MessageList messages={linesFromHistory([dm({ roll_status: "captured", roll: mixed })])} pending={null} />));
    const first = container.querySelector('[aria-label^="Attack 1"]');
    const second = container.querySelector('[aria-label^="Attack 2"]');
    expect(first?.textContent).toContain("Failure");
    expect(first?.textContent).not.toContain("dmg");
    expect(second?.textContent).toContain("Success");
    expect(container.textContent).toContain("Total damage: 7");
    expect(container.textContent!.indexOf("Second Wind")).toBeLessThan(container.textContent!.indexOf("Attack 1"));
    await act(() => root.unmount());
  });
});
