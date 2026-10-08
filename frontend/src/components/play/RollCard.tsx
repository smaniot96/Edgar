import type { AdjudicationResult } from "../../api/types";
import { cn } from "../../lib/cn";
import { rollParts, rollSummary } from "./rollText";

/**
 * Compact dice card shown above the narration it explains:
 * "Athletics · d20+3 = 17 vs DC 15 → Success · 6 dmg". Renders nothing when no dice were rolled.
 */
export function RollCard({ adj }: { adj: AdjudicationResult | null | undefined }) {
  const p = rollParts(adj);
  if (!p) return null;
  const conditions = (adj?.conditions ?? []).filter(Boolean);
  return (
    <div
      className="inline-flex max-w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-line-strong/80 bg-surface/80 px-2.5 py-1 text-xs text-ink"
      aria-label={rollSummary(p)}
      title={adj?.mechanical_summary || undefined}
    >
      <span aria-hidden className="text-ember-400">
        ⚄
      </span>
      <span className="font-semibold text-ink">{p.check}</span>
      <span aria-hidden className="text-ink-subtle">·</span>
      <span className="font-mono tabular-nums">
        {p.expression ? <span className="text-ink-muted">{p.expression} = </span> : null}
        <span className="font-semibold text-ink">{p.total}</span>
        {p.dc != null ? (
          <span className="text-ink-muted">
            {" "}
            vs {p.against} {p.dc}
          </span>
        ) : null}
      </span>
      {p.natural ? (
        <span
          className={cn(
            "rounded px-1.5 py-px text-[0.65rem] font-bold uppercase tracking-wide",
            p.natural === "nat 20" ? "bg-ember-500/20 text-ember-300" : "bg-danger-soft text-danger",
          )}
        >
          {p.natural}
        </span>
      ) : null}
      <span
        className={cn(
          "rounded px-1.5 py-px font-semibold",
          p.success ? "bg-success-soft text-success" : "bg-danger-soft text-danger",
        )}
      >
        {p.success ? "Success" : "Failure"}
      </span>
      {p.damage != null ? (
        <span className="font-mono text-ember-300">{p.damage} dmg</span>
      ) : null}
      {conditions.map((c) => (
        <span key={c} className="rounded border border-line-strong px-1.5 py-px text-ink-muted">
          {c}
        </span>
      ))}
    </div>
  );
}
