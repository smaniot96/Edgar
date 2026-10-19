import { Link } from "react-router-dom";

import { cn } from "../../lib/cn";
import { stageLabel, type TurnActivity, type TurnGate } from "./tableState";

/**
 * The play screen's turn affordance (EDG-050): who holds the spotlight, in words and colour that
 * never rely on colour alone. The visible text is not a live region; the play screen announces turn
 * changes once through its own polite region, so a change is never read twice.
 */
export function TurnBanner({
  gate,
  activity,
  tableHref,
}: {
  gate: TurnGate;
  activity: TurnActivity | null;
  tableHref: string;
}) {
  if (gate.kind === "free") return null;
  const tone =
    gate.kind === "acting" || gate.kind === "open"
      ? "border-success/60 bg-success-soft/50 text-ink"
      : gate.kind === "spectating"
        ? "border-info/50 bg-info-soft/40 text-ink"
        : "border-line-strong bg-surface-raised text-ink";
  const stage = stageLabel(activity?.stage);
  const acting = activity?.actor?.display_name ?? activity?.actor?.character_name ?? null;
  return (
    <div data-testid="turn-banner" data-gate={gate.kind} className={cn("shrink-0 border-t px-3 py-2 text-sm sm:px-4", tone)}>
      <div className="mx-auto flex w-full max-w-3xl flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <div className="min-w-0">
          <p className="flex items-center gap-2 font-semibold">
            <span aria-hidden="true" className={cn("inline-block h-2 w-2 rounded-full", gate.canAct ? "bg-success" : "bg-ink-subtle")} />
            {gate.title}
          </p>
          <p className="text-xs text-ink-muted">
            {activity && acting ? `${acting} is ${stage ?? "taking a turn"}… ` : ""}
            {gate.detail}
          </p>
        </div>
        <Link
          to={tableHref}
          className="inline-flex min-h-11 items-center rounded-control px-2 text-xs font-medium text-ember-300 underline underline-offset-2 hover:text-ember-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400"
        >
          Open the table
        </Link>
      </div>
    </div>
  );
}

/** Replaces the action composer when the viewer cannot act. There is deliberately no text input. */
export function ReadOnlyComposer({ gate }: { gate: TurnGate }) {
  return (
    <div
      data-testid="read-only-composer"
      className="shrink-0 border-t border-line bg-canvas/80 px-3 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] text-center text-sm text-ink-muted sm:px-4"
    >
      {gate.kind === "spectating"
        ? "Spectating: the action box is hidden. You can still use the journal, the map and the character sheet."
        : "Your action box will return when the spotlight reaches you."}
    </div>
  );
}
