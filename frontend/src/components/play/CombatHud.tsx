import type { CombatState } from "../../api/types";
import { cn } from "../../lib/cn";
import { economyLine, orderCombatants } from "./gameState";
import { HpBar } from "../ui/HpBar";

/** Plain-language label for a non-standing combatant. `defeated` never claims death or capture. */
const DISPOSITION_LABEL: Record<string, string> = {
  unconscious: "unconscious",
  captured: "captive",
  dead: "dead",
  defeated: "defeated",
};

/** Initiative-ordered combat tracker: every combatant incl. the player, HP, AC, whose turn. */
export function CombatHud({ combat }: { combat: CombatState }) {
  const rows = orderCombatants(combat);
  const current = rows.find((r) => r.isCurrent);
  const economy = economyLine(combat.economy);
  return (
    <section
      aria-label="Combat"
      className="shrink-0 border-b border-danger-strong/40 bg-gradient-to-b from-danger-soft/50 to-canvas/60 px-3 py-2 sm:px-4"
    >
      <div className="mx-auto w-full max-w-3xl">
        <div className="mb-1.5 flex items-center gap-2 text-[0.7rem] font-semibold uppercase tracking-wider">
          <span className="text-danger">⚔ Combat</span>
          <span className="text-ink-muted">Round {combat.round ?? 1}</span>
          {current ? (
            <span className="ml-auto normal-case tracking-normal text-ember-300">
              {current.isPlayer ? "Your turn" : `${current.name}'s turn`}
            </span>
          ) : null}
        </div>
        {economy && !combat.ended ? (
          <p className="mb-1.5 text-[0.7rem] text-ink-muted" aria-label="Hero action economy">
            {economy}
          </p>
        ) : null}
        <ol className="flex gap-2 overflow-x-auto pb-1">
          {rows.map((c) => (
            <li
              key={c.key}
              aria-current={c.isCurrent ? "true" : undefined}
              className={cn(
                "flex w-40 shrink-0 flex-col gap-1 rounded-md border px-2 py-1.5 text-xs",
                c.isPlayer ? "border-ember-800/70 bg-ember-950/20" : "border-line bg-surface/80",
                c.isCurrent && "ring-1 ring-ember-400",
                (!c.alive || c.canAct === false) && "opacity-50",
              )}
            >
              <div className="flex items-center gap-1.5">
                {c.isCurrent ? <span aria-hidden className="text-ember-400">▶</span> : null}
                <span className={cn("truncate font-semibold text-ink", !c.alive && "line-through")}>
                  {c.name}
                </span>
                {c.isPlayer ? <span className="text-[0.6rem] uppercase text-ember-400/80">you</span> : null}
                {c.disposition && DISPOSITION_LABEL[c.disposition] ? (
                  <span className="text-[0.6rem] uppercase text-ink-muted">{DISPOSITION_LABEL[c.disposition]}</span>
                ) : null}
                <span className="ml-auto flex shrink-0 gap-1.5 font-mono text-[0.65rem] text-ink-muted">
                  {c.init != null ? <span title="Initiative">⚡{c.init}</span> : null}
                  {c.ac != null ? <span title="Armor class">AC {c.ac}</span> : null}
                </span>
              </div>
              <HpBar current={c.hp} max={c.hpMax} compact label={`${c.name} HP`} />
              {c.effects.length ? (
                <ul aria-label={`${c.name} active effects`} className="flex flex-wrap gap-1">
                  {c.effects.map((e) => (
                    <li
                      key={e.kind}
                      className="rounded border border-danger/50 bg-danger-soft/40 px-1.5 py-px text-[0.6rem] font-semibold uppercase tracking-wide text-danger"
                    >
                      {e.label}
                    </li>
                  ))}
                </ul>
              ) : null}
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
