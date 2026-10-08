import { isRouteErrorResponse, Link, useRouteError } from "react-router-dom";

import { Button, ButtonLink } from "../components/ui/Button";
import { errorDetails, errorMessage } from "../api/client";
import { useMode } from "../dev/ModeContext";

/**
 * Themed router `errorElement`: shown when a route throws while rendering/loading.
 * `fullScreen` is used for the root boundary (no app shell around it).
 */
export default function RouteError({ fullScreen = false }: { fullScreen?: boolean }) {
  const error = useRouteError();
  const { isDev } = useMode();

  const notFound = isRouteErrorResponse(error) && error.status === 404;
  const title = notFound ? "This path leads nowhere" : "Something went wrong";
  const message = notFound
    ? "There's no page here. The map may be out of date."
    : isRouteErrorResponse(error)
      ? `${error.status} ${error.statusText}`
      : "Edgar hit an unexpected error while drawing this page. Reloading usually helps.";
  const details = isDev
    ? isRouteErrorResponse(error)
      ? JSON.stringify(error.data, null, 2)
      : `${errorMessage(error)}\n\n${errorDetails(error) ?? ""}`
    : null;

  return (
    <div
      role="alert"
      className={
        fullScreen
          ? "flex min-h-dvh items-center justify-center bg-canvas p-4 text-ink"
          : "flex min-h-0 flex-1 items-center justify-center overflow-y-auto p-4"
      }
    >
      <div className="w-full max-w-md rounded-card border border-line bg-surface p-6 text-center">
        <h1 className="font-display text-display-md text-ink">{title}</h1>
        <p className="mt-2 text-sm text-ink-muted">{message}</p>
        <div className="mt-5 flex flex-wrap justify-center gap-2">
          <ButtonLink to="/campaigns" reloadDocument={fullScreen}>
            Back to Campaigns
          </ButtonLink>
          <Button variant="secondary" onClick={() => window.location.reload()}>
            Reload
          </Button>
        </div>
        {fullScreen ? (
          <Link to="/" reloadDocument className="mt-3 inline-block text-xs text-ink-subtle hover:text-ink">
            Edgar home
          </Link>
        ) : null}
        {details ? (
          <pre className="mt-4 max-h-60 overflow-auto whitespace-pre-wrap break-words text-left font-mono text-[11px] text-ink-subtle">
            {details}
          </pre>
        ) : null}
      </div>
    </div>
  );
}
