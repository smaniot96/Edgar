import { errorDetails, errorMessage, isOfflineError } from "../../api/client";
import { useMode } from "../../dev/ModeContext";
import { cn } from "../../lib/cn";
import { Button } from "./Button";

/**
 * Player-friendly error line. Raw status/body/request id appear only in Developer mode.
 * Pass `onRetry` to offer a retry button.
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
  const { isDev } = useMode();
  if (error == null) return null;
  const details = isDev ? errorDetails(error) : null;
  return (
    <div
      role="alert"
      className={cn(
        "rounded-control border border-danger-strong/60 bg-danger-soft/60 px-3 py-2 text-sm text-danger",
        className,
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p>{errorMessage(error)}</p>
        {onRetry ? (
          <Button variant="secondary" size="sm" onClick={onRetry}>
            Retry
          </Button>
        ) : null}
      </div>
      {details ? (
        <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-ink-muted">
          {details}
        </pre>
      ) : null}
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
  const { isDev } = useMode();
  const offline = isOfflineError(error);
  const details = isDev ? errorDetails(error) : null;
  return (
    <div role="alert" className="mx-auto max-w-md rounded-card border border-line bg-surface p-6 text-center">
      <h2 className="font-display text-display-sm text-ink">
        {title ?? (offline ? "Edgar is offline" : "Couldn't load this page")}
      </h2>
      <p className="mt-2 text-sm text-ink-muted">{errorMessage(error)}</p>
      {offline ? (
        <p className="mt-1 text-xs text-ink-subtle">
          Start the API (for example with <code className="font-mono">make up</code>) and retry.
        </p>
      ) : null}
      <Button className="mt-4" onClick={onRetry}>
        Retry
      </Button>
      {details ? (
        <pre className="mt-4 max-h-40 overflow-auto whitespace-pre-wrap break-words text-left font-mono text-[11px] text-ink-subtle">
          {details}
        </pre>
      ) : null}
    </div>
  );
}
