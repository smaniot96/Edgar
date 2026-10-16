import { useState } from "react";

import { listCharacters } from "../../api/client";
import type { PreparationSetup } from "../../api/types";
import { ErrorNotice } from "../../components/ui/ErrorNotice";
import { Field, TextField } from "../../components/ui/Field";
import { useQuery } from "../../lib/useQuery";

/** Only setup/preferences are player decisions. All adventure content stays with the DM. */
export function usePreparationSetup() {
  const characters = useQuery("preparation-heroes", listCharacters);
  const [heroId, setHeroId] = useState("");
  const [target, setTarget] = useState("");
  const [budget, setBudget] = useState("");
  const hero = characters.data?.find((character) => String(character.id) === heroId);
  const amount = Number(budget);
  const setup: PreparationSetup | undefined = hero && Number.isFinite(amount) && amount > 0
    ? {
        character_ids: [hero.id],
        target_level: target ? Number(target) : hero.level,
        max_cost_microusd: Math.round(amount * 1_000_000),
        required_maps: true,
        required_handouts: true,
      }
    : undefined;

  const fields = (
    <fieldset className="space-y-3 rounded-card border border-line p-3">
      <legend className="px-1 text-sm font-medium">Your adventure setup</legend>
      <p className="text-sm text-ink-muted">
        Edgar privately prepares and checks every act for your selected hero, using verified
        books. Duration and character level are separate. Required maps and handouts are
        included; unavailable rules or books will explain what needs attention.
      </p>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Hero" required hint={hero ? `Starts at level ${hero.level}` : "Choose a saved character"}>
          {(props) => <select {...props} required value={heroId} onChange={(event) => setHeroId(event.target.value)} className="min-h-11 rounded-control border border-line bg-surface p-2 text-sm">
            <option value="">{characters.loading ? "Loading heroes…" : "Choose a hero"}</option>
            {(characters.data ?? []).map((character) => <option key={character.id} value={character.id}>
              {character.name} — level {character.level} {character.character_class}
            </option>)}
          </select>}
        </Field>
        <TextField label="Target level (optional)" type="number" min={hero?.level ?? 1} max={20}
          value={target} onChange={(event) => setTarget(event.target.value)}
          placeholder={hero ? String(hero.level) : "Same as starting level"} />
        <TextField label="Preparation budget (USD)" required type="number" min="0.01" max="1000" step="0.01"
          value={budget} onChange={(event) => setBudget(event.target.value)} placeholder="Maximum spend" />
      </div>
      {!characters.loading && characters.data?.length === 0 && <p className="text-sm text-ink-muted">
        Create a hero in Characters, then return to prepare your adventure.
      </p>}
      <ErrorNotice error={characters.error} onRetry={characters.refetch} />
      <p className="text-xs text-ink-muted">Preparation stops at this budget. It may need a retry;
        this limit is not a promise about cost or completion time.</p>
    </fieldset>
  );
  return { setup, fields };
}
