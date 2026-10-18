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

/**
 * Next step for a gate. Wording is the same in local single-user and token mode: the frontend has no
 * separate-host signal, and the player's own campaigns are the only ones listed. Review gates are
 * left to the blocker's own message, which already says to review and publish.
 */
function nextStepFor(action: ReadinessRead["primary_action"]): string | null {
  switch (action) {
    case "recover_source":
      return "Restore the bound adventure source before playing.";
    case "repair_preparation":
      return "Repair campaign preparation before playing.";
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
  // Each distinct sentence appears once, even when a blocker and the next step say the same thing.
  const [headline, ...details] = [...new Set([...blockers.map((blocker) => blocker.message), nextStepFor(value.primary_action)])]
    .filter((message): message is string => !!message);
  return (
    <section
      role={announce ? "status" : undefined}
      aria-live={announce ? "polite" : undefined}
      className={`rounded-control border border-warning/30 bg-warning-soft px-3 py-2 text-sm text-ink ${className}`}
    >
      <p className="font-medium">{headline}</p>
      {details.length ? (
        <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-ink-muted">
          {details.map((message) => <li key={message}>{message}</li>)}
        </ul>
      ) : null}
      {!value.history_allowed ? <p className="mt-1 text-xs text-ink-muted">Saved history is unavailable.</p> : null}
      {value.primary_action === "retry_readiness" && onRetry ? (
        <Button variant="secondary" size="sm" className="mt-2 min-h-11" onClick={onRetry}>Check again</Button>
      ) : null}
    </section>
  );
}

export { UNKNOWN as UNKNOWN_READINESS };
