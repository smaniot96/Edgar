import type { AdjudicationResult } from "../../api/types";

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
  };
}

/** One-line plain-text version, e.g. "Athletics · 1d20+5 = 17 vs DC 15 → Success · 6 dmg". */
export function rollSummary(p: RollParts): string {
  let s = `${p.check} · ${p.expression ? `${p.expression} = ` : ""}${p.total}`;
  if (p.natural) s += ` (${p.natural})`;
  if (p.dc != null) s += ` vs ${p.against} ${p.dc}`;
  s += ` → ${p.success ? "Success" : "Failure"}`;
  if (p.damage != null) s += ` · ${p.damage} dmg`;
  return s;
}
