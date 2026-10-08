import { useEffect } from "react";

import { cn } from "../../lib/cn";
import { abilityModifier, formatModifier, type SheetCharacter } from "./gameState";
import { HpBar } from "./HpBar";

function SectionTitle({ children }: { children: string }) {
  return (
    <h3 className="mb-2 text-[0.65rem] font-semibold uppercase tracking-[0.18em] text-ember-500/80">
      {children}
    </h3>
  );
}

/**
 * Character sheet. On large screens it is a sidebar beside the chat; on small screens an
 * overlay drawer (Escape or the backdrop closes it).
 */
export function CharacterSheet({
  character,
  open,
  onClose,
}: {
  character: SheetCharacter | null;
  open: boolean;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

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
        id="character-sheet"
        aria-label="Character sheet"
        className={cn(
          "fixed inset-y-0 right-0 z-40 flex w-[min(20rem,88vw)] flex-col border-l border-line bg-canvas shadow-2xl shadow-black/60",
          "lg:static lg:z-auto lg:w-72 lg:shadow-none",
        )}
      >
        <div className="flex items-start gap-2 border-b border-line px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 className="truncate font-display text-display-sm text-ink">{c.name || "Adventurer"}</h2>
            <p className="text-xs text-ink-muted">
              {[c.className, c.level != null ? `Level ${c.level}` : null].filter(Boolean).join(" · ") || "—"}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close character sheet"
            className="rounded p-1 text-ink-muted hover:bg-surface-raised hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400"
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
                          <li key={`${it}-${i}`} className="before:mr-1.5 before:text-ember-600/70 before:content-['•']">
                            {it}
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
