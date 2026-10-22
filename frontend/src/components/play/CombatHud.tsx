import type { CombatState } from "../../api/types";
import { cn } from "../../lib/cn";
import { economyLine, orderCombatants } from "./gameState";
import { HpBar } from "../ui/HpBar";

/** Plain-language label for a non-standing combatant. `defeated` never claims death or capture. */
// Distance bands from the hero (EDG-044): within or beyond 5 feet.
const BAND_LABEL: Record<string, string> = {
  melee: "in melee",
  ranged: "at range",
};

const DISPOSITION_LABEL: Record<string, string> = {
  unconscious: "unconscious",
  captured: "captive",
  dead: "dead",
  defeated: "defeated",
};

/** Kept beside the composer in the desktop workspace; reusable by the full HUD. */
export function CombatDeclaration({ combat, onReply }: { combat: CombatState; onReply?: (text: string) => void }) {
  const pending = combat.ended ? null : combat.pending_declaration;
  if (!pending) return null;
  return <div role="group" aria-label="Confirm declaration" className="mx-auto max-h-44 w-full max-w-3xl overflow-y-auto rounded-md border border-ember-300/60 bg-surface px-3 py-2 text-sm">
    <p className="mb-2 whitespace-pre-wrap break-words text-ink">{pending.summary}</p>
    <div className="flex gap-2">
      <button type="button" onClick={() => onReply?.("yes")} disabled={!onReply} className="min-h-9 rounded border border-ember-300 px-3 py-1 text-xs font-semibold text-ember-300 disabled:opacity-50">Confirm</button>
      <button type="button" onClick={() => onReply?.("no")} disabled={!onReply} className="min-h-9 rounded border border-ink-muted/40 px-3 py-1 text-xs text-ink-muted disabled:opacity-50">Cancel and change</button>
    </div>
  </div>;
}

export function EncounterDanger({ combat, compact = false }: { combat: CombatState; compact?: boolean }) {
  const text = combat.solo_budget?.warning?.text;
  if (!text || combat.ended) return null;
  return <details className="mb-1 rounded border border-danger-strong/60 bg-danger-soft/60 px-2 py-1 text-xs text-ink" open={!compact}>
    <summary className="cursor-pointer font-semibold text-danger">Danger: {combat.solo_budget?.warning?.tier?.replaceAll("_", " ") || "severe encounter"}</summary>
    <p className="mt-1 break-words">{text}</p>
  </details>;
}

/** Initiative-ordered combat tracker: every combatant incl. the player, HP, AC, whose turn. */
export function CombatHud({
  combat,
  onReply,
  compact = false,
  showDeclaration = true,
  onOpenEncounter,
}: {
  combat: CombatState;
  /** Sends a reply as the player's next turn (the confirmation's yes or cancel). */
  onReply?: (text: string) => void;
  compact?: boolean;
  showDeclaration?: boolean;
  onOpenEncounter?: () => void;
}) {
  const rows = orderCombatants(combat);
  const current = rows.find((r) => r.isCurrent);
  const economy = economyLine(combat.economy);
  const orders = combat.ended ? [] : (combat.standing_orders ?? []);
  return (
    <section
      aria-label="Combat"
      className="shrink-0 border-b border-danger-strong/40 bg-gradient-to-b from-danger-soft/50 to-canvas/60 px-3 py-2 sm:px-4"
    >
      <div className="mx-auto w-full max-w-3xl">
        <div className="mb-1.5 flex items-center gap-2 text-[0.7rem] font-semibold uppercase tracking-wider">
          <span className="text-danger">⚔ Combat</span>
          <span className="text-ink-muted">Round {combat.round ?? 1}</span>
          {onOpenEncounter ? <button type="button" onClick={onOpenEncounter} className="rounded px-2 py-1 normal-case tracking-normal text-ember-300 underline underline-offset-2">Encounter view</button> : null}
          {current ? (
            <span className="ml-auto break-words text-right normal-case tracking-normal text-ember-300">
              {current.isPlayer ? "Your turn" : `${current.name}${current.actorContext ? ` (${current.actorContext})` : ""}'s turn`}
            </span>
          ) : null}
        </div>
        <EncounterDanger combat={combat} compact={compact} />
        {economy && !combat.ended ? (
          <p className="mb-1.5 text-[0.7rem] text-ink-muted" aria-label="Hero action economy">
            {economy}
          </p>
        ) : null}
        {orders.length > 0 ? (
          <ul aria-label="Standing orders" className="mb-2 space-y-1 text-sm text-ink">
            {orders.map((order) => (
              <li key={order.foe} className="flex flex-wrap items-center gap-2">
                <span>{order.summary}.</span>
                <button
                  type="button"
                  onClick={() => onReply?.(`cancel the standing order against ${order.foe}`)}
                  disabled={!onReply}
                  className="rounded border border-ink-muted/40 px-2 py-0.5 text-xs text-ink-muted disabled:opacity-50"
                >
                  Cancel order
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        {showDeclaration ? <CombatDeclaration combat={combat} onReply={onReply} /> : null}
        {!compact ? <ol className="flex gap-2 overflow-x-auto pb-1">
          {rows.map((c) => (
            <li
              key={c.key}
              aria-label={`${c.isPlayer ? "Player" : "Combatant"}: ${c.name}${c.actorContext ? `, ${c.actorContext}` : ""}`}
              aria-current={c.isCurrent ? "true" : undefined}
              className={cn(
                "flex w-40 shrink-0 flex-col gap-1 rounded-md border px-2 py-1.5 text-xs",
                c.isPlayer ? "border-ember-800/70 bg-ember-950/20" : "border-line bg-surface/80",
                c.isCurrent && "ring-1 ring-ember-400",
                (!c.alive || c.canAct === false) && "opacity-50",
              )}
            >
              <div className="flex flex-wrap items-center gap-1.5">
                {c.isCurrent ? <span aria-hidden className="text-ember-400">▶</span> : null}
                <span className={cn("min-w-0 whitespace-normal break-words font-semibold text-ink", !c.alive && "line-through")}>
                  {c.name}
                </span>
                {c.isPlayer ? <span className="shrink-0 text-[0.6rem] uppercase text-ember-400/80">you</span> : null}
                {!c.isPlayer && c.actorContext ? <span className="shrink-0 text-[0.6rem] uppercase text-ink-muted">{c.actorContext}</span> : null}
                {c.disposition && DISPOSITION_LABEL[c.disposition] ? (
                  <span className="text-[0.6rem] uppercase text-ink-muted">{DISPOSITION_LABEL[c.disposition]}</span>
                ) : null}
                {c.band ? (
                  <span
                    aria-label={`${c.name} position`}
                    className="text-[0.6rem] uppercase text-ink-muted"
                  >
                    {BAND_LABEL[c.band] ?? c.band}
                  </span>
                ) : null}
                <span className="ml-auto flex shrink-0 gap-1.5 font-mono text-[0.65rem] text-ink-muted">
                  {c.init != null ? <span title="Initiative">⚡{c.init}</span> : null}
                  {c.ac != null ? <span title="Armor class">AC {c.ac}</span> : null}
                </span>
              </div>
              <HpBar current={c.hp} max={c.hpMax} compact label={`${c.name}${c.actorContext ? `, ${c.actorContext}` : ""} HP`} />
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
        </ol> : <p className="line-clamp-2 text-xs text-ink-muted">{rows.map((c) => `${c.name}${c.actorContext ? ` (${c.actorContext})` : ""}: ${c.hp ?? "?"}/${c.hpMax ?? "?"} HP${c.band ? ` · ${BAND_LABEL[c.band] ?? "position unknown"}` : ""}`).join(" · ")}</p>}
      </div>
    </section>
  );
}
