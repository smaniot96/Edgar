import type { AdjudicationResult, DiceLogEntry, RollRecord } from "../../api/types";

export interface RollParts {
  check: string;
  expression: string | null;
  total: number;
  dc: number | null;
  /** Label for the target number: "DC" for checks/saves, "AC" for attacks. */
  against: "DC" | "AC";
  /** "nat 20" / "nat 1" when the raw d20 face is known and critical. */
  natural: "nat 20" | "nat 1" | null;
  success: boolean;
  damage: number | null;
  /** Both d20 faces when advantage or disadvantage applied; null for a plain roll. */
  faces: number[] | null;
  mode: "advantage" | "disadvantage" | null;
}

/** The engine's advantage/disadvantage d20 entry for the check, when one was rolled. */
function modeEntry(adj: AdjudicationResult): DiceLogEntry | null {
  const entry = (adj.dice_log ?? []).find(
    (d) => (d.roll_mode === "advantage" || d.roll_mode === "disadvantage") && d.rolls.length === 2,
  );
  return entry ?? null;
}

/** Extract what the roll card shows; null when no dice were rolled this turn. */
export function rollParts(adj: AdjudicationResult | null | undefined): RollParts | null {
  if (!adj || typeof adj.dice_result !== "number") return null;
  let expression: string | null = adj.roll_expression?.trim() || null;
  if (!expression && typeof adj.modifier === "number") {
    expression = adj.modifier === 0 ? "d20" : `d20${adj.modifier > 0 ? "+" : "−"}${Math.abs(adj.modifier)}`;
  }
  return {
    check: adj.check?.trim() || "Roll",
    expression,
    total: adj.dice_result,
    dc: typeof adj.dc === "number" ? adj.dc : null,
    against: adj.against === "AC" ? "AC" : "DC",
    natural: adj.natural_roll === 20 ? "nat 20" : adj.natural_roll === 1 ? "nat 1" : null,
    success: !!adj.success,
    damage: typeof adj.damage === "number" && adj.damage > 0 ? adj.damage : null,
    ...rollModeFields(adj),
  };
}

function rollModeFields(adj: AdjudicationResult): Pick<RollParts, "faces" | "mode"> {
  const entry = modeEntry(adj);
  if (!entry || !entry.roll_mode) return { faces: null, mode: null };
  return { faces: entry.rolls, mode: entry.roll_mode };
}

/** One-line plain-text version, e.g. "Athletics · 1d20+5 = 17 vs DC 15 → Success · 6 dmg". */
export function rollSummary(p: RollParts): string {
  const faces = p.faces ? ` [${p.faces.join(", ")}]` : "";
  let s = `${p.check} · ${p.expression ? `${p.expression}${faces} = ` : ""}${p.total}`;
  if (p.natural) s += ` (${p.natural})`;
  if (p.dc != null) s += ` vs ${p.against} ${p.dc}`;
  s += ` → ${p.success ? "Success" : "Failure"}`;
  if (p.damage != null) s += ` · ${p.damage} dmg`;
  return s;
}

/**
 * The roll card's input for a stored turn (EDG-030): the same numbers the live turn showed.
 * Only the fields the card and its dev details read are copied.
 */
export function adjudicationFromRoll(roll: RollRecord): AdjudicationResult {
  return {
    success: roll.success,
    damage: roll.damage,
    conditions: roll.conditions,
    mechanical_summary: roll.mechanical_summary,
    dice_result: roll.dice_result,
    check: roll.check,
    dc: roll.dc,
    against: roll.against,
    modifier: roll.modifier,
    roll_expression: roll.roll_expression,
    natural_roll: roll.natural_roll,
    dice_log: roll.dice_log,
  };
}
