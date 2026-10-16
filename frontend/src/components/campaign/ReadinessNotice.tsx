import type { ReadinessRead } from "../../api/types";
import { Button } from "../ui/Button";

const UNKNOWN: ReadinessRead = {
  play_allowed: false,
  history_allowed: true,
  state: "readiness_unavailable",
  primary_action: "retry_readiness",
  blockers: [{
    code: "readiness_unavailable",
    message: "Campaign readiness could not be checked. Retry before starting a turn.",
    action: "retry_readiness",
  }],
};

function hostAction(action: ReadinessRead["primary_action"]): string | null {
  switch (action) {
    case "review_source":
    case "review_adventure":
      return "Ask the campaign host to review and publish the adventure.";
    case "recover_source":
      return "Ask the campaign host to restore the bound source.";
    case "repair_preparation":
      return "Ask the campaign host to repair campaign preparation.";
    case "wait_for_preparation":
      return "Preparation is still running. Check back when it finishes.";
    case "choose_character":
      return "Choose a character assigned to this campaign before continuing.";
    case "read_history":
      return "The saved story remains available to read.";
    default:
      return null;
  }
}

/** Player-safe explanation for a campaign gate. It never exposes preparation internals. */
export function ReadinessNotice({
  readiness,
  onRetry,
  className = "",
  announce = true,
}: {
  readiness: ReadinessRead | null | undefined;
  onRetry?: () => void;
  className?: string;
  /** Turn off live announcement when a second, dedicated notice carries the update. */
  announce?: boolean;
}) {
  const value = readiness ?? UNKNOWN;
  if (value.play_allowed) return null;
  const blockers = value.blockers.length ? value.blockers : UNKNOWN.blockers;
  const nextStep = hostAction(value.primary_action);
  return (
    <section
      role={announce ? "status" : undefined}
      aria-live={announce ? "polite" : undefined}
      className={`rounded-control border border-warning/30 bg-warning-soft px-3 py-2 text-sm text-ink ${className}`}
    >
      <p className="font-medium">{blockers[0].message}</p>
      {blockers.length > 1 ? (
        <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-ink-muted">
          {blockers.slice(1).map((blocker) => <li key={blocker.code}>{blocker.message}</li>)}
        </ul>
      ) : null}
      {nextStep ? <p className="mt-1 text-xs text-ink-muted">{nextStep}</p> : null}
      {!value.history_allowed ? <p className="mt-1 text-xs text-ink-muted">Saved history is unavailable.</p> : null}
      {value.primary_action === "retry_readiness" && onRetry ? (
        <Button variant="secondary" size="sm" className="mt-2" onClick={onRetry}>Check again</Button>
      ) : null}
    </section>
  );
}

export { UNKNOWN as UNKNOWN_READINESS };
