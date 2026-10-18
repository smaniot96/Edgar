import { useRef } from "react";

import type { CastableSpell } from "../../api/types";
import { cn } from "../../lib/cn";
import { abilityModifier, formatModifier, type SheetCharacter } from "./gameState";
import { HpBar } from "../ui/HpBar";
import { ResourceCounters } from "./ResourceCounters";
import { useModalFocus } from "./useModalFocus";
import { useNarrowViewport } from "./useNarrowViewport";

function SectionTitle({ children }: { children: string }) {
  return (
    <h3 className="mb-2 text-[0.65rem] font-semibold uppercase tracking-[0.18em] text-ember-500/80">
      {children}
    </h3>
  );
}

const ORDINALS = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th"];

/** Plain-language cost: a cantrip needs no slot; a leveled spell names the slot it spends. */
function spellCost(spell: CastableSpell): string {
  const cost = spell.level === 0 ? "Cantrip" : `${ORDINALS[spell.level - 1] ?? spell.level}-level slot`;
  return spell.action === "bonus_action" ? `${cost} · bonus action` : cost;
}

/** "sleight_of_hand" to "Sleight of hand". */
function skillLabel(skill: string): string {
  const words = skill.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Character sheet. On large screens it is a sidebar beside the chat; on small screens an
 * overlay drawer (Escape or the backdrop closes it).
 */
export function CharacterSheet({
  character,
  open,
  onClose,
  readableItems,
  onReadItem,
}: {
  character: SheetCharacter | null;
  open: boolean;
  onClose: () => void;
  /** Exact inventory name to the exact journal clue entry id. */
  readableItems?: ReadonlyMap<string, string>;
  onReadItem?: (entryId: string) => void;
}) {
  const narrow = useNarrowViewport();
  const sheetRef = useRef<HTMLElement | null>(null);
  useModalFocus(open && !!character, narrow, onClose, sheetRef);

  if (!open || !character) return null;
  const c = character;

  return (
    <>
      <div
        aria-hidden
        onClick={onClose}
        className="fixed inset-0 z-30 bg-black/50 lg:hidden"
      />
      <aside
        ref={sheetRef}
        id="character-sheet"
        aria-label="Character sheet"
        role={narrow ? "dialog" : undefined}
        aria-modal={narrow ? true : undefined}
        tabIndex={narrow ? -1 : undefined}
        className={cn(
          "fixed inset-y-0 right-0 z-40 flex w-[min(20rem,88vw)] flex-col border-l border-line bg-canvas pt-[env(safe-area-inset-top)] shadow-2xl shadow-black/60",
          "lg:static lg:z-auto lg:w-72 lg:shadow-none",
        )}
      >
        <div className="flex items-start gap-2 border-b border-line px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 className="whitespace-normal break-words font-display text-display-sm text-ink [overflow-wrap:anywhere]">{c.name || "Adventurer"}</h2>
            <p className="text-xs text-ink-muted">
              {[c.className, c.level != null ? `Level ${c.level}` : null].filter(Boolean).join(" · ") || "—"}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close character sheet"
            className="min-h-11 min-w-11 shrink-0 rounded p-1 text-ink-muted hover:bg-surface-raised hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400"
          >
            ✕
          </button>
        </div>

        <div className="flex-1 space-y-5 overflow-y-auto px-4 py-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
          <section className="flex items-center gap-3">
            <HpBar current={c.hpCurrent} max={c.hpMax} className="flex-1" />
            {c.ac != null ? (
              <div
                className="flex h-10 w-10 shrink-0 flex-col items-center justify-center rounded-md border border-line-strong bg-surface"
                title="Armor class"
              >
                <span className="text-[0.55rem] font-semibold uppercase text-ink-subtle">AC</span>
                <span className="font-mono text-sm font-semibold leading-none text-ink">{c.ac}</span>
              </div>
            ) : null}
          </section>

          {c.hitDice ? (
            <section
              className="flex items-baseline justify-between text-sm"
              title="A short rest spends the Hit Dice you declare, one if you name none; a long rest recovers up to half your Hit Dice, rounded down."
            >
              <span className="text-xs text-ink-subtle">Hit Dice</span>
              <span className="font-mono text-ink">
                {c.hitDice.remaining}/{c.hitDice.total} {c.hitDice.die}
              </span>
            </section>
          ) : null}

          {c.abilities.length ? (
            <section>
              <SectionTitle>Abilities</SectionTitle>
              <dl className="grid grid-cols-3 gap-1.5">
                {c.abilities.map((a) => (
                  <div
                    key={a.key}
                    className="flex flex-col items-center rounded-md border border-line bg-surface/70 py-1.5"
                  >
                    <dt className="text-[0.6rem] font-semibold tracking-wider text-ink-subtle">{a.key}</dt>
                    <dd className="font-mono text-base font-semibold leading-tight text-ink">
                      {formatModifier(abilityModifier(a.score))}
                    </dd>
                    <dd className="font-mono text-[0.65rem] text-ink-subtle">{a.score}</dd>
                  </div>
                ))}
              </dl>
            </section>
          ) : null}

          {c.secondWind || c.spellSlots?.length ? (
            <section aria-label="Resources">
              <SectionTitle>Resources</SectionTitle>
              <ResourceCounters secondWind={c.secondWind} spellSlots={c.spellSlots} />
            </section>
          ) : null}

          {c.spells?.length ? (
            <section aria-label="Spells">
              <SectionTitle>Spells</SectionTitle>
              <ul className="space-y-1 text-sm">
                {c.spells.map((spell) => (
                  <li key={spell.name} className="flex items-baseline justify-between gap-2">
                    <span className="min-w-0 break-words text-ink">{spell.name}</span>
                    <span className="shrink-0 text-xs text-ink-subtle">{spellCost(spell)}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {c.proficiencies?.saving_throws.length || c.proficiencies?.skills.length ? (
            <section aria-label="Proficiencies">
              <SectionTitle>Proficiencies</SectionTitle>
              <dl className="space-y-2 text-sm">
                {c.proficiencies.saving_throws.length ? (
                  <div>
                    <dt className="text-xs text-ink-subtle">Saving throws</dt>
                    <dd>
                      <ul className="space-y-0.5 text-ink">
                        {c.proficiencies.saving_throws.map((save) => (
                          <li key={save.ability} className="flex justify-between gap-2">
                            <span>{save.ability}</span>
                            <span className="font-mono">{formatModifier(save.modifier)}</span>
                          </li>
                        ))}
                      </ul>
                    </dd>
                  </div>
                ) : null}
                {c.proficiencies.skills.length ? (
                  <div>
                    <dt className="text-xs text-ink-subtle">Skills</dt>
                    <dd>
                      <ul className="space-y-0.5 text-ink">
                        {c.proficiencies.skills.map((skill) => (
                          <li key={skill.skill} className="flex justify-between gap-2">
                            <span>{skillLabel(skill.skill)}</span>
                            <span className="font-mono">{formatModifier(skill.modifier)}</span>
                          </li>
                        ))}
                      </ul>
                    </dd>
                  </div>
                ) : null}
              </dl>
            </section>
          ) : null}

          <section>
            <SectionTitle>Conditions</SectionTitle>
            {c.conditions.length ? (
              <ul className="flex flex-wrap gap-1.5">
                {c.conditions.map((x) => (
                  <li key={x} className="rounded-full border border-ember-800/70 bg-ember-950/40 px-2 py-0.5 text-xs text-ember-200">
                    {x}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs italic text-ink-subtle">None</p>
            )}
          </section>

          <section>
            <SectionTitle>Inventory</SectionTitle>
            {c.inventory.length ? (
              <dl className="space-y-2 text-sm">
                {c.inventory.map((g) => (
                  <div key={g.label}>
                    <dt className="text-xs text-ink-subtle">{g.label}</dt>
                    <dd>
                      <ul className="space-y-0.5 text-ink">
                        {g.items.map((it, i) => (
                        <li key={`${it}-${i}`} className="flex min-w-0 items-center gap-2 before:shrink-0 before:text-ember-600/70 before:content-['•']">
                          <span className="min-w-0 flex-1 whitespace-normal break-words [overflow-wrap:anywhere]">{it}</span>
                          {readableItems?.get(it) && onReadItem ? (
                            <button
                              type="button"
                              aria-label={`Read ${it}`}
                              className="min-h-11 shrink-0 px-2 text-xs font-medium text-ember-300 underline decoration-ember-400 underline-offset-2 hover:text-ember-200"
                              onClick={() => onReadItem(readableItems.get(it)!)}
                            >
                              Read
                            </button>
                          ) : null}
                          </li>
                        ))}
                      </ul>
                    </dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="text-xs italic text-ink-subtle">Empty</p>
            )}
          </section>
        </div>
      </aside>
    </>
  );
}
