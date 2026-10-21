import { useState } from "react";
import { Link } from "react-router-dom";

import { assignCharacterToCampaign, levelUpCampaignCharacter, listCampaignCharacters, listCharacters } from "../../api/client";
import type { CampaignCharacterRead, LifeState, ProgressionMode } from "../../api/types";
import { Button } from "../../components/ui/Button";
import { HpBar } from "../../components/ui/HpBar";
import { EmptyState, LoadingText } from "../../components/ui/EmptyState";
import { ErrorNotice } from "../../components/ui/ErrorNotice";
import { cardClasses, linkClasses } from "../../components/ui/styles";
import { useMode } from "../../dev/ModeContext";
import { cn } from "../../lib/cn";
import { useQuery } from "../../lib/useQuery";

/** The level milestone advancement reaches (EDG-045, agent.progression.MAX_ADVANCED_LEVEL). */
const MAX_ADVANCED_LEVEL = 5;
/** The level at which the Ability Score Improvement choice is taken (PHB, PDF p.73). */
const ASI_LEVEL = 4;
const ABILITIES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"] as const;

const LIFE_LABEL: Record<Exclude<LifeState, "alive">, string> = {
  dying: "Dying: making death saving throws",
  stable: "Stable: unconscious, no longer at risk this moment",
  dead: "Dead: a permanent loss. Add a replacement hero from the Characters page.",
};

function newRequestId(): string {
  // crypto.randomUUID is absent on plain-HTTP origins, so keep a fallback that is still unique.
  return globalThis.crypto?.randomUUID?.() ?? `lvl-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export default function CharactersTab({
  campaignId,
  progressionMode = "fixed",
  ended = false,
}: {
  campaignId: number;
  progressionMode?: ProgressionMode;
  ended?: boolean;
}) {
  const { isDev } = useMode();
  const chars = useQuery(`campaign-characters:${campaignId}`, (s) =>
    listCampaignCharacters(campaignId, s),
  );
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notices, setNotices] = useState<Record<number, string>>({});
  const [ability, setAbility] = useState<Record<number, string>>({});
  const available = useQuery(ended ? null : "characters", listCharacters);
  const [adding, setAdding] = useState(false);
  const [selection, setSelection] = useState("");

  async function addCharacter() {
    if (!selection || adding) return;
    setAdding(true);
    setError(null);
    try {
      await assignCharacterToCampaign(campaignId, Number(selection));
      setSelection("");
      chars.refetch();
      available.refetch();
    } catch (cause) { setError(cause); available.refetch(); } finally { setAdding(false); }
  }

  async function levelUp(c: CampaignCharacterRead) {
    setBusyId(c.character_id);
    setError(null);
    const toLevel = c.level + 1;
    const choice = ability[c.character_id] ?? "STR";
    try {
      const result = await levelUpCampaignCharacter(campaignId, c.character_id, {
        from_level: c.level,
        request_id: newRequestId(),
        hp_method: "average",
        ...(toLevel === ASI_LEVEL ? { ability_increases: { [choice]: 2 } } : {}),
      });
      const gained = result.features.filter((f) => f.executed).map((f) => f.name);
      const named = result.features.map((f) => f.name);
      const text = [
        `Level ${result.to_level}: maximum HP ${result.hit_points.hp_max_before} to ${result.hit_points.hp_max_after}.`,
        gained.length ? `Running now: ${gained.join(", ")}.` : null,
        named.length > gained.length ? `Named, not run by the engine: ${named.filter((n) => !gained.includes(n)).join(", ")}.` : null,
      ]
        .filter(Boolean)
        .join(" ");
      setNotices((prev) => ({ ...prev, [c.character_id]: text }));
      chars.refetch();
    } catch (err) {
      setError(err);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-ink-muted">
        Every character in this campaign's party. Add available characters here, or create more on the{" "}
        <Link to="/characters" className={linkClasses}>
          Characters
        </Link>{" "}
        page.
      </p>
      {!ended ? <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); void addCharacter(); }}>
        <label className="min-w-0 flex-1 text-xs text-ink-muted">Add to the party
          <select value={selection} disabled={adding} onChange={(event) => setSelection(event.target.value)} className="mt-1 block min-h-11 w-full rounded-control border border-line bg-surface px-3 text-sm text-ink">
            <option value="">Choose an available character</option>
            {available.data?.filter((character) => !character.current_assignment).map((character) => <option key={character.id} value={character.id}>{character.name} · Level {character.level} {character.character_class}</option>)}
          </select>
        </label>
        <Button type="submit" loading={adding} disabled={!selection}>Add character</Button>
      </form> : null}
      {available.error ? <ErrorNotice error={available.error} onRetry={available.refetch} /> : null}
      {error ? <ErrorNotice error={error} /> : null}
      {chars.error && chars.data === undefined ? (
        <ErrorNotice error={chars.error} onRetry={chars.refetch} />
      ) : chars.data === undefined ? (
        <LoadingText />
      ) : chars.data.length === 0 ? (
        <EmptyState title="No characters here yet">
          Add an available character to this campaign's party.
        </EmptyState>
      ) : (
        <ul className="space-y-2">
          {chars.data.map((c) => {
            const life = c.life_state ?? "alive";
            const pending = c.milestones_pending ?? 0;
            const canLevel =
              progressionMode === "milestone" && pending > 0 && life === "alive" && c.level < MAX_ADVANCED_LEVEL;
            return (
              <li key={c.assignment_id} className={cn(cardClasses, "px-4 py-3")}>
                <p className="text-sm font-medium">
                  {c.name}
                  {isDev ? (
                    <span className="ml-2 font-mono text-xs text-ink-subtle">
                      char #{c.character_id} · assignment #{c.assignment_id}
                    </span>
                  ) : null}
                </p>
                <p className="text-xs text-ink-muted">
                  Level {c.level} {c.character_class} · HP {c.hp_current}/{c.hp_max}
                </p>
                {life !== "alive" ? (
                  <p role="status" className={cn("mt-1 text-xs font-medium", life === "dead" ? "text-danger-strong" : "text-ember-500")}>
                    {LIFE_LABEL[life]}
                  </p>
                ) : null}
                <HpBar current={c.hp_current} max={c.hp_max} compact showValue={false} label="Hit points" className="mt-1.5 w-32" />
                {canLevel ? (
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <p className="text-xs text-ink-muted">
                      {pending} milestone{pending === 1 ? "" : "s"} to spend.
                    </p>
                    {c.level + 1 === ASI_LEVEL ? (
                      <label className="text-xs text-ink-muted">
                        <span className="mr-1">Ability score +2</span>
                        <select
                          className="rounded-control border border-line bg-surface px-2 py-1 text-xs"
                          value={ability[c.character_id] ?? "STR"}
                          onChange={(e) => setAbility((prev) => ({ ...prev, [c.character_id]: e.target.value }))}
                        >
                          {ABILITIES.map((a) => (
                            <option key={a} value={a}>{a}</option>
                          ))}
                        </select>
                      </label>
                    ) : null}
                    <Button
                      size="sm"
                      loading={busyId === c.character_id}
                      loadingText="Levelling up…"
                      disabled={busyId != null}
                      onClick={() => void levelUp(c)}
                    >
                      Level up to {c.level + 1}
                    </Button>
                  </div>
                ) : null}
                {notices[c.character_id] ? (
                  <p role="status" className="mt-2 text-xs text-ink-muted">{notices[c.character_id]}</p>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
