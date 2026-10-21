import { useState } from "react";

import { cancelAdventure, retryAdventure } from "../../api/client";
import type { AdventureRead } from "../../api/types";
import {
  isJobActive,
  isPublicationCommitting,
  preparationProgress,
} from "../../lib/adventureJob";
import { Pill } from "../Pill";
import { Button } from "../ui/Button";
import { ErrorNotice } from "../ui/ErrorNotice";
import { ExtractionPreview } from "./ExtractionPreview";
import { PreparationRecovery } from "./PreparationRecovery";
import { ReadinessNotice } from "./ReadinessNotice";

export function AdventureStatusBadge({
  adventure,
}: {
  adventure: AdventureRead;
}) {
  if (adventure.status === "processing" || isJobActive(adventure.job))
    return <Pill tone="info">Preparing</Pill>;
  if (
    adventure.status === "ready" &&
    adventure.readiness?.play_allowed === true
  )
    return <Pill tone="success">Ready</Pill>;
  if (adventure.status === "failed")
    return <Pill tone="warning">Preparation needs attention</Pill>;
  return <Pill tone="warning">Needs attention</Pill>;
}

/** Preparation controls belong to the adventure, never to a particular playthrough. */
export function AdventurePreparation({
  adventure,
  onUpdated,
}: {
  adventure: AdventureRead;
  onUpdated: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const job = adventure.job;
  const running = adventure.status === "processing" || isJobActive(job);
  const retryable = job
    ? job.retryable &&
      ["failed", "interrupted", "cancelled"].includes(job.state)
    : adventure.can_retry;

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onUpdated();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  if (
    !running &&
    adventure.status === "ready" &&
    adventure.readiness?.play_allowed === true
  )
    return null;
  return (
    <section
      className="space-y-3 rounded-card border border-line bg-surface-sunken p-5"
      aria-label="Adventure preparation"
    >
      <h2 className="font-display text-xl">
        {running ? "Preparing your adventure" : "Preparation needs attention"}
      </h2>
      {running ? (
        <p role="status" className="text-sm text-ink-muted">
          {preparationProgress(job) ||
            "Preparation is in progress. You can leave this page; it keeps working."}
        </p>
      ) : null}
      {job?.failure ? <ErrorNotice error={job.failure} /> : null}
      {job?.explanation ? (
        <p className="text-sm text-ink-muted">{job.explanation}</p>
      ) : null}
      {!running && !job?.failure && !job?.explanation && adventure.readiness ? (
        <ReadinessNotice readiness={adventure.readiness} onRetry={onUpdated} />
      ) : null}
      {adventure.extraction_preview_available ? (
        <ExtractionPreview
          slug={adventure.slug}
          onUsePreview={
            retryable && adventure.extraction_preview_current
              ? (sourceHash, extractionHash) =>
                  run(() =>
                    retryAdventure(adventure.slug, {
                      reviewed_source_sha256: sourceHash,
                      reviewed_extraction_sha256: extractionHash,
                    }),
                  )
              : undefined
          }
        />
      ) : null}
      <div className="flex flex-wrap gap-2">
        {adventure.preparation_setup_required ? (
          <Button onClick={() => setSetupOpen(true)}>Set up preparation</Button>
        ) : retryable ? (
          <Button
            loading={busy}
            onClick={() => void run(() => retryAdventure(adventure.slug))}
          >
            {job?.action_label || "Retry preparation"}
          </Button>
        ) : null}
        {running &&
        job &&
        job.state !== "cancel_requested" &&
        !isPublicationCommitting(job) ? (
          <Button
            variant="secondary"
            loading={busy}
            onClick={() => void run(() => cancelAdventure(adventure.slug))}
          >
            Cancel preparation
          </Button>
        ) : null}
        <Button variant="ghost" onClick={onUpdated}>
          Check again
        </Button>
      </div>
      <ErrorNotice error={error} />
      <PreparationRecovery
        open={setupOpen}
        slug={adventure.slug}
        edition={adventure.rules_edition}
        onClose={() => setSetupOpen(false)}
        onStarted={onUpdated}
      />
    </section>
  );
}
