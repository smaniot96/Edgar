import { useState } from "react";

import { errorMessage } from "../../api/client";
import { PACING_POLICIES, setPacingPolicy } from "../../api/tablePacing";
import type { PacingPolicyName, PacingState } from "../../api/tablePacing";
import type { SpotlightState } from "../../api/sessionChannel";
import { cn } from "../../lib/cn";
import { Pill } from "../Pill";

/**
 * The table's pacing policy (EDG-051). Everyone sees the current policy and what it means; the host
 * (`canChange`) can switch it. The server announces the change to the whole table, so the lobby
 * updates `pacing` from the channel; `onChanged` also receives the fresh state from the PATCH.
 *
 * Mount (EDG-050 lobby): `<PacingControls sessionId={id} pacing={spotlight.pacing} canChange={isOwner}
 * onChanged={setSpotlight} />`, next to the spotlight mode controls.
 */
export function PacingControls({
  sessionId,
  pacing,
  canChange,
  onChanged,
  className,
}: {
  sessionId: number;
  pacing: PacingState | undefined;
  canChange: boolean;
  onChanged?: (state: SpotlightState) => void;
  className?: string;
}) {
  const [busy, setBusy] = useState<PacingPolicyName | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!pacing) return null;

  const choose = async (policy: PacingPolicyName) => {
    if (busy || policy === pacing.policy) return;
    setBusy(policy);
    setError(null);
    try {
      onChanged?.(await setPacingPolicy(sessionId, policy));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className={cn("space-y-2", className)} aria-label="Table pacing">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-ink">Table pacing</h3>
        <Pill tone="info">{pacing.label}</Pill>
      </div>
      <p className="text-sm text-ink-muted">{pacing.summary}</p>
      {canChange ? (
        <div role="radiogroup" aria-label="Pacing policy" className="grid gap-2 sm:grid-cols-3">
          {PACING_POLICIES.map((option) => {
            const selected = option.value === pacing.policy;
            return (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={selected}
                disabled={busy !== null}
                onClick={() => void choose(option.value)}
                className={cn(
                  "rounded-control border px-3 py-2 text-left text-sm transition-colors",
                  selected ? "border-ember-500 bg-surface-raised" : "border-line-strong bg-surface hover:border-ember-700",
                )}
              >
                <span className="block font-semibold text-ink">{option.label}</span>
                <span className="block text-xs text-ink-muted">{option.blurb}</span>
              </button>
            );
          })}
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      ) : null}
    </section>
  );
}
