import type { SecondWindState } from "../../api/types";
import type { SpellSlotRow } from "./gameState";

/** Filled and empty pips for a count; decorative only, the text carries the number. */
function Pips({ filled, total }: { filled: number; total: number }) {
  return (
    <span aria-hidden className="font-mono text-[0.7rem] tracking-[0.15em] text-ember-400">
      {"●".repeat(filled)}
      <span className="text-ink-subtle">{"○".repeat(Math.max(0, total - filled))}</span>
    </span>
  );
}

/**
 * Engine-owned resource counters for the character sheet. Renders nothing when the character has
 * neither resource (legacy payloads, non-fighters, non-casters). Hit Dice are not shown: not tracked yet.
 */
export function ResourceCounters({
  secondWind,
  spellSlots,
}: {
  secondWind: SecondWindState | null;
  spellSlots: SpellSlotRow[] | null;
}) {
  if (!secondWind && !spellSlots?.length) return null;
  return (
    <div className="space-y-2.5">
      {secondWind ? (
        <div className="flex items-center justify-between gap-2 text-sm">
          <span className="text-ink">Second Wind</span>
          <span className="flex items-center gap-2 text-xs text-ink-muted">
            <Pips filled={secondWind.uses_remaining} total={secondWind.uses_max} />
            <span>
              {secondWind.uses_remaining} of {secondWind.uses_max}{" "}
              {secondWind.uses_max === 1 ? "use" : "uses"} left
            </span>
          </span>
        </div>
      ) : null}
      {spellSlots?.length ? (
        <ul aria-label="Spell slots" className="space-y-1.5 text-sm">
          {spellSlots.map((slot) => (
            <li key={slot.level} className="flex items-center justify-between gap-2">
              <span className="text-ink">Level {slot.level} slots</span>
              <span className="flex items-center gap-2 text-xs text-ink-muted">
                <Pips filled={slot.remaining} total={slot.total} />
                <span>
                  {slot.remaining} of {slot.total} left
                </span>
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
