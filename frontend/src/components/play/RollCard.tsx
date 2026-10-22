import type { AdjudicationResult } from "../../api/types";
import { cn } from "../../lib/cn";
import { healingRolls, multipleAttackRolls, restSummary, rollParts, rollSummary, type HealingRoll } from "./rollText";

const CONTAINER =
  "inline-flex max-w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-line-strong/80 bg-surface/80 px-2.5 py-1 text-xs text-ink";

function healingSummary(roll: HealingRoll) {
  if (typeof roll.applied === "number") {
    return `${roll.label} · ${roll.expression}${roll.rolls.length ? ` [${roll.rolls.join(", ")}]` : ""} = ${roll.total} rolled · ${roll.applied} HP restored`;
  }
  return `${roll.label} · ${roll.expression}${roll.rolls.length ? ` [${roll.rolls.join(", ")}]` : ""} = ${roll.total}${roll.total < 0 ? ", heals 0" : " healing"}`;
}

/**
 * Preserve raw dice and distinguish recorded applied healing after the HP cap.
 * A total below zero is the roll's arithmetic and heals nothing, so it never reads as negative healing.
 */
function HealingPill({ roll }: { roll: HealingRoll }) {
  return (
    <span className="rounded border border-success/40 px-1.5 py-px font-mono text-success">
      {healingSummary(roll)}
    </span>
  );
}

/**
 * Compact dice card shown above the narration it explains:
 * "Athletics · d20+3 = 17 vs DC 15 → Success · 6 dmg". Healing dice appear around the check in
 * recorded order when the main check has a unique dice-log match; legacy cards keep their order.
 */
export function RollCard({ adj }: { adj: AdjudicationResult | null | undefined }) {
  const attacks = multipleAttackRolls(adj);
  if (adj && attacks.length) {
    const log = adj.dice_log ?? [];
    return (
      <div className="flex max-w-full flex-col items-start gap-1" aria-label="Attack sequence" title={adj.mechanical_summary || undefined}>
        {attacks.map((attack, index) => (
          <div key={attack.index} className="flex max-w-full flex-wrap items-center gap-1">
            {healingRolls({ ...adj, dice_log: log.slice(index ? attacks[index - 1].index + 1 : 0, attack.index) }).map((heal, position) => <HealingPill key={position} roll={heal} />)}
            <RollCard adj={attack.adjudication} />
          </div>
        ))}
        {typeof adj.damage === "number" && adj.damage > 0 ? <span className="text-xs font-mono text-ember-300">Total damage: {adj.damage}</span> : null}
        {(adj.conditions ?? []).filter(Boolean).map((condition) => <span key={condition} className="text-xs text-ink-muted">{condition}</span>)}
        {healingRolls({ ...adj, dice_log: log.slice(attacks[attacks.length - 1].index + 1) }).map((heal, index) => <HealingPill key={index} roll={heal} />)}
      </div>
    );
  }
  const p = rollParts(adj);
  const rest = adj?.rest_record ?? null;
  if (!p && rest) {
    // A rest turn has no check; its card shows only the engine's rest evidence (EDG-026).
    const text = restSummary(rest);
    return (
      <div className={CONTAINER} aria-label={text}>
        <span aria-hidden className="text-ember-400">
          ⚄
        </span>
        <span className="font-mono tabular-nums text-ink">{text}</span>
      </div>
    );
  }
  const heals = healingRolls(adj);
  if (!p && !heals.length) return null;
  const healPills = heals.map((h, i) => <HealingPill key={`${h.label}-${i}`} roll={h} />);
  if (!p) {
    return (
      <div className={CONTAINER} title={adj?.mechanical_summary || undefined}>
        {healPills}
      </div>
    );
  }
  const log = adj?.dice_log ?? [];
  const matchingChecks = log.flatMap((entry, index) => entry.total === p.total && entry.expression === p.expression && !/second wind|healing|hit die/i.test(entry.purpose ?? "") ? [index] : []);
  const checkIndex = matchingChecks.length === 1 ? matchingChecks[0] : -1;
  const before = adj && checkIndex >= 0 ? healingRolls({ ...adj, dice_log: log.slice(0, checkIndex) }) : [];
  const after = adj && checkIndex >= 0 ? healingRolls({ ...adj, dice_log: log.slice(checkIndex + 1) }) : heals;
  const conditions = (adj?.conditions ?? []).filter(Boolean);
  return (
    <div
      className={CONTAINER}
      aria-label={[...before.map(healingSummary), rollSummary(p), ...after.map(healingSummary)].join(" · ")}
      title={adj?.mechanical_summary || undefined}
    >
      {before.map((roll, index) => <HealingPill key={`before-${index}`} roll={roll} />)}
      <span aria-hidden className="text-ember-400">
        ⚄
      </span>
      <span className="font-semibold text-ink">{p.check}</span>
      <span aria-hidden className="text-ink-subtle">·</span>
      <span className="font-mono tabular-nums">
        {p.expression ? (
          <span className="text-ink-muted">
            {p.expression}
            {p.faces ? <span title={`${p.mode}: both d20 faces rolled`}> [{p.faces.join(", ")}]</span> : null}
            {" = "}
          </span>
        ) : null}
        <span className="font-semibold text-ink">{p.total}</span>
        {p.dc != null ? (
          <span className="text-ink-muted">
            {" "}
            vs {p.against} {p.dc}
          </span>
        ) : null}
      </span>
      {p.natural ? (
        <span
          className={cn(
            "rounded px-1.5 py-px text-[0.65rem] font-bold uppercase tracking-wide",
            p.natural === "nat 20" ? "bg-ember-500/20 text-ember-300" : "bg-danger-soft text-danger",
          )}
        >
          {p.natural}
        </span>
      ) : null}
      <span
        className={cn(
          "rounded px-1.5 py-px font-semibold",
          p.success ? "bg-success-soft text-success" : "bg-danger-soft text-danger",
        )}
      >
        {p.success ? "Success" : "Failure"}
      </span>
      {p.damage != null ? (
        <span className="font-mono text-ember-300">{p.damage} dmg</span>
      ) : null}
      {conditions.map((c) => (
        <span key={c} className="rounded border border-line-strong px-1.5 py-px text-ink-muted">
          {c}
        </span>
      ))}
      {after.map((roll, index) => <HealingPill key={`after-${index}`} roll={roll} />)}
    </div>
  );
}
