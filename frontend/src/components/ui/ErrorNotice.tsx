import { safeFailureFromUnknown, type SafeRecoveryAction } from "../../api/turnStream";
import { cn } from "../../lib/cn";
import { Button } from "./Button";

/**
 * Player-friendly error line. It uses fixed copy derived from safe categories; exception,
 * provider, and validation body text is never rendered, including in Developer mode.
 */
export function ErrorNotice({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  if (error == null) return null;
  const safe = safeFailureFromUnknown(error);
  const nextStep: Partial<Record<SafeRecoveryAction, string>> = {
    repair_source: "Open the adventure source and review its recovery options.",
    review_source: "Review and publish the current adventure material before continuing.",
    fix_input: "Check the highlighted information and correct it before continuing.",
    assign_character: "Choose a character assigned to this campaign.",
    wait: "Wait for the current action to finish, then check its status.",
    retry: "The server confirms this action was not saved, so you can retry it.",
    check_existing_result: "Check the saved result before you submit this action again.",
    configure_provider: "Review the provider configuration or allowance before continuing.",
    restart_preparation: "Review the preparation setup and start a new preparation.",
    contact_support: "Share the reference below with support if the problem continues.",
    read_history: "Your saved campaign history remains available to read.",
  };
  // Token-mode refusals (EDG-160) are about access, not the input, so they get their own next step.
  const accessStep = safe.category === "authentication"
    ? "Open Settings and enter a current access token. What you typed is still here."
    : safe.category === "forbidden"
      ? "Use an access token for an administrator account, or ask the operator for one."
      : null;
  return (
    <div
      role="alert"
      aria-atomic="true"
      className={cn(
        "rounded-control border border-danger-strong/60 bg-danger-soft/60 px-3 py-2 text-sm text-danger",
        className,
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p>{safe.message}</p>
        {onRetry ? (
          <Button variant="secondary" size="sm" onClick={onRetry}>
            Check again
          </Button>
        ) : null}
      </div>
      <p className="mt-1 text-xs text-ink-muted">{accessStep ?? nextStep[safe.recoveryAction ?? "contact_support"]}</p>
      {safe.supportRef ? <p className="mt-1 text-xs text-ink-subtle">Support reference: {safe.supportRef}</p> : null}
    </div>
  );
}

/** Full-panel state for when the API can't be reached (or a page failed to load). */
export function OfflineState({
  error,
  onRetry,
  title,
}: {
  error: unknown;
  onRetry: () => void;
  title?: string;
}) {
  const safe = safeFailureFromUnknown(error);
  const offline = safe.category === "interrupted";
  return (
    <div role="alert" className="mx-auto max-w-md rounded-card border border-line bg-surface p-6 text-center">
      <h2 className="font-display text-display-sm text-ink">
        {title ?? (offline ? "Edgar is offline" : "Couldn't load this page")}
      </h2>
      <p className="mt-2 text-sm text-ink-muted">{safe.message}</p>
      {offline ? (
        <p className="mt-1 text-xs text-ink-subtle">
          Check your connection, then check this page again.
        </p>
      ) : null}
      <Button className="mt-4" onClick={onRetry}>
        Check again
      </Button>
      <p className="mt-2 text-xs text-ink-subtle">Your saved work is still available.</p>
      {safe.supportRef ? <p className="mt-1 text-xs text-ink-subtle">Support reference: {safe.supportRef}</p> : null}
    </div>
  );
}
