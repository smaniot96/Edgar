import type { AdjudicationResult, DiceLogEntry, RestRecord, RollRecord } from "../../api/types";

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
  const check = adj.check?.toLowerCase().trim();
  const log = adj.dice_log ?? [];
  const named = log.filter((d) => d.purpose?.toLowerCase() === check || d.purpose?.toLowerCase() === `${check} check`);
  // Weapon checks have an engine-owned purpose. Never borrow a caster's roll,
  // even when its expression, bonus or total happens to match the hero's.
  const normalExpression = (value?: string | null) => value?.toLowerCase().replace(/k[hl]1/g, "").replace(/\s/g, "");
  const candidates = named.length || check === "attack" ? named : log.filter((d) =>
    normalExpression(d.expression) === normalExpression(adj.roll_expression) && d.total === adj.dice_result &&
    (adj.natural_roll == null || (d.kept ?? d.rolls[0]) === adj.natural_roll),
  );
  const entry = candidates.length === 1 ? candidates[0] : null;
  return entry && (entry.roll_mode === "advantage" || entry.roll_mode === "disadvantage") && entry.rolls.length === 2 ? entry : null;
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

/** The current weapon sequence attacks one target at its recorded AC. Each player
 * attack has its own engine d20 entry; enemy rolls and damage dice never become checks.
 * Keep aggregate applied damage separate: damage dice can precede resistance or immunity.
 */
export function multipleAttackRolls(adj: AdjudicationResult | null | undefined): Array<{ index: number; adjudication: AdjudicationResult }> {
  if (!adj || adj.against !== "AC" || typeof adj.dc !== "number" || adj.check !== "attack") return [];
  const attacks = (adj.dice_log ?? []).flatMap((entry, index) => entry.purpose === "attack check" ? [{ entry, index }] : []);
  if (attacks.length < 2) return [];
  return attacks.map(({ entry, index }, position) => {
    const natural = entry.kept ?? (entry.rolls.length === 1 ? entry.rolls[0] : null);
    return {
      index,
      adjudication: {
        ...adj,
        check: `Attack ${position + 1}`,
        dice_result: entry.total,
        roll_expression: entry.expression,
        natural_roll: natural,
        modifier: entry.modifier,
        success: natural === 20 || (natural !== 1 && entry.total >= adj.dc!),
        damage: null,
        conditions: [],
        dice_log: [entry],
      },
    };
  });
}

/** A healing dice roll from the engine's dice log (Second Wind, spell healing, item or rest healing). */
export interface HealingRoll {
  /** Dice-log purpose, capitalised for display, e.g. "Second Wind" or "Cure Wounds healing". */
  label: string;
  expression: string;
  rolls: number[];
  total: number;
  applied?: number;
}

/** Dice-log purposes that are healing rolls: Second Wind, "<spell> healing", "healing (<item>)", hit dice. */
const HEALING_PURPOSE = /second wind|healing|hit die/i;

/** Healing dice the engine rolled this turn, in log order. Only logged dice are shown. */
export function healingRolls(adj: AdjudicationResult | null | undefined): HealingRoll[] {
  const out: HealingRoll[] = [];
  for (const d of adj?.dice_log ?? []) {
    const purpose = typeof d.purpose === "string" ? d.purpose.trim() : "";
    if (!purpose || !HEALING_PURPOSE.test(purpose) || typeof d.total !== "number") continue;
    out.push({
      label: /^second wind$/i.test(purpose) ? "Second Wind" : purpose.charAt(0).toUpperCase() + purpose.slice(1),
      expression: d.expression,
      rolls: Array.isArray(d.rolls) ? d.rolls : [],
      total: d.total,
      ...(typeof d.applied === "number" ? { applied: d.applied } : {}),
    });
  }
  return out;
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
 * One-line rest evidence (EDG-026), e.g. "Short rest · 1d10+2 = 7 · healed 7 HP · Hit Dice 0/1 d10 left".
 * Shows the engine's numbers only; a refused rest says so and rolls nothing.
 */
export function restSummary(rest: RestRecord): string {
  const left = `Hit Dice ${rest.hit_dice_remaining}/${rest.hit_dice_total} ${rest.die} left`;
  if (rest.kind === "long") {
    const recovered = rest.hit_dice_recovered ?? 0;
    return `Long rest · HP restored · ${recovered > 0 ? `recovered ${recovered} Hit ${recovered === 1 ? "Die" : "Dice"} · ` : ""}${left}`;
  }
  if (rest.refused) return `Short rest · no Hit Dice left · no healing · ${left}`;
  const roll = rest.expression && rest.rolled != null ? `${rest.expression} = ${rest.rolled} · ` : "";
  return `Short rest · ${roll}healed ${rest.healed} HP · ${left}`;
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
    rest_record: roll.rest ?? null,
  };
}
