import { useState } from "react";
import type { CombatState } from "../../api/types";
import { orderCombatants, type HudCombatant } from "./gameState";
import { HpBar } from "../ui/HpBar";
import { EncounterDanger } from "./CombatHud";
import { TacticalGrid } from "./TacticalGrid";

/** A schematic of engine-owned engagements. Card layout conveys no geometry or scale. */
export function EncounterView({ combat, onSelectTarget, sessionId, onPrepareMove }: {
  combat: CombatState;
  onSelectTarget?: (name: string) => void;
  sessionId?: number;
  onPrepareMove?: (text: string) => void;
}) {
  const rows = orderCombatants(combat);
  const visibleNonparticipants = combat.participants?.filter((participant) => participant.participation === "visible_nonparticipant") ?? [];
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = rows.find((row) => row.key === selectedId);
  const groups: { label: string; rows: HudCombatant[] }[] = [
    { label: "Your party", rows: rows.filter((row) => row.isPlayer) },
    { label: "In melee", rows: rows.filter((row) => !row.isPlayer && row.band === "melee") },
    { label: "At range", rows: rows.filter((row) => !row.isPlayer && row.band === "ranged") },
    { label: "Position unknown", rows: rows.filter((row) => !row.isPlayer && row.band !== "melee" && row.band !== "ranged") },
  ];
  return <section aria-label="Encounter schematic" className="space-y-4 overflow-y-auto p-4 text-sm">
    <div>
      <h3 className="font-display text-lg font-semibold">Round {combat.round} · {combat.ended ? "Encounter ended" : "Current engagement"}</h3>
      {!combat.battlefield ? <p className="mt-1 text-xs text-ink-muted">Schematic, not to scale. Positions show each foe’s engagement with the hero. Card spacing does not represent distance, terrain, or cover.</p> : null}
    </div>
    <EncounterDanger combat={combat} />
    {combat.battlefield ? <TacticalGrid combat={combat} sessionId={sessionId} onPrepareMove={onPrepareMove} onInspect={(id) => setSelectedId(rows.find((row) => row.key === id || row.name === id)?.key ?? null)} /> : null}
    {groups.filter((group) => group.rows.length).map((group) => <section key={group.label} aria-label={group.label}>
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">{group.label}</h4>
      <ol className="grid gap-2 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
        {group.rows.map((row) => <li key={row.key}>
          <button type="button" aria-pressed={selectedId === row.key} aria-label={`Inspect ${row.name}${row.actorContext ? ` (${row.actorContext})` : ""}`} onClick={() => setSelectedId(row.key)} className={`w-full rounded-card border p-3 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400 ${selectedId === row.key ? "border-ember-400 bg-ember-950/40" : "border-line bg-surface"}`}>
            <span className="flex gap-2"><strong className="min-w-0 flex-1 break-words">{row.name}</strong>{row.isCurrent ? <span className="text-xs text-ember-300">Acting</span> : null}</span>
            {row.actorContext ? <span className="text-xs text-ink-muted">{row.actorContext}</span> : null}
            <HpBar current={row.hp} max={row.hpMax} compact label={`${row.name} HP`} />
            <span className="mt-1 block text-xs text-ink-muted">{row.init != null ? `Initiative ${row.init} · ` : ""}{row.ac != null ? `AC ${row.ac}` : "AC unknown"}{row.disposition && row.disposition !== "active" ? ` · ${row.disposition}` : ""}</span>
            {row.effects.length ? <span className="mt-1 block text-xs text-danger">{row.effects.map((effect) => effect.label).join(" · ")}</span> : null}
          </button>
        </li>)}
      </ol>
    </section>)}
    {!rows.length ? <p className="text-ink-muted">No combatants are recorded for this encounter.</p> : null}
    {visibleNonparticipants.length ? <section aria-label="Visible nonparticipants">
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">Visible, outside initiative</h4>
      <ul className="space-y-2">{visibleNonparticipants.map((participant) => <li key={participant.id} className="rounded-card border border-line bg-surface p-3">
        <p className="break-words font-semibold">{participant.name}</p>
        <p className="mt-1 break-words text-xs text-ink-muted">{participant.reason}</p>
      </li>)}</ul>
    </section> : null}
    {combat.round_events?.length ? <section aria-label="Last resolved round">
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">Last resolved round</h4>
      <ol className="list-decimal space-y-1 pl-5 text-ink-muted">{combat.round_events.slice(0, 30).map((event, index) => <li key={`${index}:${event.kind}`} className="break-words">{event.text.slice(0, 1000)}</li>)}</ol>
    </section> : null}
    {selected ? <section aria-label="Selected combatant" className="rounded-card border border-ember-600/50 bg-surface px-3 py-3">
      <p className="break-words font-semibold">{selected.name}{selected.actorContext ? ` (${selected.actorContext})` : ""}</p>
      <p className="mt-1 text-xs text-ink-muted">Inspection spends no turn. Add this name to your draft, then describe what you want to do.</p>
      {!selected.isPlayer ? <button type="button" disabled={!onSelectTarget || combat.ended} onClick={() => onSelectTarget?.(selected.name)} className="mt-2 min-h-10 rounded border border-ember-500 px-3 py-1 text-sm text-ember-300 disabled:opacity-50">Add target to draft</button> : null}
    </section> : null}
  </section>;
}
