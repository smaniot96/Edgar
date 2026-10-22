import type { SecondWindState } from "../../api/types";
import type { ClassFeatureRow, SpellSlotRow } from "./gameState";

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
 * no tracked resources. Hit Dice have their own section in the sheet.
 */
export function ResourceCounters({
  secondWind,
  classFeatures,
  spellSlots,
}: {
  secondWind: SecondWindState | null;
  classFeatures?: ClassFeatureRow[] | null;
  spellSlots: SpellSlotRow[] | null;
}) {
  if (!secondWind && !classFeatures?.length && !spellSlots?.length) return null;
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
      {classFeatures?.map((feature) => (
        <div key={feature.key} className="flex items-center justify-between gap-2 text-sm">
          <span className="text-ink">{feature.label}</span>
          <span className="flex items-center gap-2 text-xs text-ink-muted">
            <Pips filled={feature.uses_remaining} total={feature.uses_max} />
            <span>
              {feature.uses_remaining} of {feature.uses_max}{" "}
              {feature.uses_max === 1 ? "use" : "uses"} left
            </span>
          </span>
        </div>
      ))}
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
