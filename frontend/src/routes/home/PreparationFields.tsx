import { useState } from "react";

import type { PreparationSetup } from "../../api/types";
import { TextField } from "../../components/ui/Field";

/** Module preparation is independent of the hero selected for a later playthrough. */
export function usePreparationSetup() {
  const [budget, setBudget] = useState("5");
  const amount = Number(budget);
  const setup: PreparationSetup | undefined = Number.isFinite(amount) && amount >= 0.01 && amount <= 1000
    ? {
        cost_target_microusd: Math.round(amount * 1_000_000),
      }
    : undefined;

  const fields = (
    <fieldset className="space-y-3 rounded-card border border-line p-3">
      <legend className="px-1 text-sm font-medium">Your adventure setup</legend>
      <p className="text-sm text-ink-muted">
        Edgar privately prepares and checks a reusable adventure using verified books.
        Choose or create your hero when you start a playthrough; that hero is always the
        story’s protagonist. Source level guidance and original encounters stay with the module.
        Maps and reference material are included where useful.
      </p>
      <TextField label="Preparation cost target (USD)" required type="number" min="0.01" max="1000" step="0.01"
        value={budget} onChange={(event) => setBudget(event.target.value)} placeholder="Cost target" />
      <p className="text-xs text-ink-muted">This target is used to compare actual cost afterward.
        Preparation continues past it; it does not limit spending or guarantee completion time.</p>
      <p className="text-xs text-ink-muted">The current rules engine supports 2014 rules.
        Encounters adapt to your chosen hero during play; unavailable source rules or books
        explain what needs attention during preparation.</p>
    </fieldset>
  );
  return { setup, fields };
}
