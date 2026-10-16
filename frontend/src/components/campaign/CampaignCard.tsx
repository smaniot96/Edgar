import { useState } from "react";

import { cancelAdventure, retryAdventure } from "../../api/client";
import type { AdventureRead, ReadinessRead } from "../../api/types";
import { safeTurnError } from "../../api/turnStream";
import { ReadinessNotice } from "./ReadinessNotice";
import { useMode } from "../../dev/ModeContext";
import type { CardModel } from "../../lib/campaignCards";
import { isPublicationCommitting, jobFailureHeading, preparationProgress } from "../../lib/adventureJob";
import { cn } from "../../lib/cn";
import { relativeTime } from "../../lib/format";
import { Pill } from "../Pill";
import { Button } from "../ui/Button";
import { ErrorNotice } from "../ui/ErrorNotice";
import { PlayIcon } from "../ui/icons";
import { cardClasses } from "../ui/styles";
import { ExtractionPreview } from "./ExtractionPreview";
import { PreparationRecovery } from "./PreparationRecovery";

function StatusBadge({ state, readiness }: { state: CardModel["state"]; readiness: ReadinessRead | null }) {
  if (readiness && !readiness.play_allowed && state === "ready") {
    return <Pill tone="warning">Needs attention</Pill>;
  }
  switch (state) {
    case "ready":
      return <Pill tone="success">Ready</Pill>;
    case "ended":
      return <Pill tone="neutral">Ended</Pill>;
    case "recovery":
      return <Pill tone="danger">Source unavailable</Pill>;
    case "failed":
      return <Pill tone="danger">Failed</Pill>;
    default:
      return (
        <Pill tone="info">
          <span aria-hidden className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-info" />
          Processing
        </Pill>
      );
  }
}

function Cover({ adventure, name }: { adventure: AdventureRead; name: string }) {
  if (adventure.cover_image) {
    return (
      <img
        src={`/${adventure.cover_image}`}
        alt=""
        className="h-32 w-full rounded-t-card object-cover"
      />
    );
  }
  const initial = name.replace(/^(the|a|an)\s+/i, "").trim().charAt(0).toUpperCase() || "E";
  return (
    <div
      aria-hidden
      className="relative flex h-32 w-full items-center justify-center overflow-hidden rounded-t-card bg-gradient-to-br from-ember-950/70 via-surface-sunken to-canvas"
    >
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(60% 70% at 50% 110%, rgba(255,107,43,0.28), transparent 70%)",
        }}
      />
      <span className="relative font-display text-6xl font-bold text-ember-300/80">{initial}</span>
    </div>
  );
}

export function CampaignCard({
  card,
  onPlay,
  onDelete,
  opening,
  deleting,
  onRetried,
}: {
  card: CardModel;
  onPlay: (card: CardModel) => void;
  onDelete: (card: CardModel) => void;
  opening: boolean;
  deleting: boolean;
  onRetried?: () => void;
}) {
  const { isDev } = useMode();
  const [retrying, setRetrying] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [retryError, setRetryError] = useState<unknown>(null);
  const { adventure, name, state, lastPlayed } = card;
  const job = adventure.job;
  const jobRunning = job != null && ["queued", "running", "cancel_requested"].includes(job.state);
  const playable = !!card.campaign || state === "ready" || state === "ended";
  const showAdventureTitle = name !== adventure.title;
  const recoverable = state === "failed" || state === "recovery";
  const canRetryJob = job
    ? job.retryable && ["interrupted", "failed", "cancelled"].includes(job.state)
    : recoverable && !!adventure.can_retry;
  const canReviewCurrentPdf = canRetryJob && adventure.extraction_preview_current &&
    (job?.kind === "ingestion" || adventure.ingestion?.source_kind === "pdf");

  async function retry(reviewed?: { reviewed_source_sha256: string; reviewed_extraction_sha256: string }) {
    setRetrying(true);
    setRetryError(null);
    try {
      await retryAdventure(adventure.slug, reviewed);
      onRetried?.();
    } catch (error) {
      setRetryError(error);
    } finally {
      setRetrying(false);
    }
  }

  async function cancel() {
    setCancelling(true);
    setRetryError(null);
    try {
      await cancelAdventure(adventure.slug);
      onRetried?.();
    } catch (error) {
      setRetryError(error);
    } finally {
      setCancelling(false);
    }
  }

  const progress = preparationProgress(job);
  const failure = job?.failure ?? (adventure.failure_code ? { code: adventure.failure_code } : null);
  const failureMessage = failure ? safeTurnError(failure).message : null;

  return (
    <article className={cn(cardClasses, "flex flex-col")}>
      <Cover adventure={adventure} name={name} />
      <div className="flex flex-1 flex-col p-4">
        <div className="mb-1 flex items-start justify-between gap-2">
          <h3 className="font-display text-display-sm leading-tight">{name}</h3>
          <StatusBadge state={state} readiness={card.readiness} />
        </div>
        {showAdventureTitle ? (
          <p className="mb-1 text-xs text-ink-muted">{adventure.title}</p>
        ) : null}
        <p className="mb-2 text-xs text-ink-subtle">
          {lastPlayed ? `Last played ${relativeTime(lastPlayed)}` : "Not played yet"}
        </p>
        {adventure.description ? (
          <p className="mb-2 line-clamp-2 text-sm text-ink-muted">{adventure.description}</p>
        ) : null}
        {card.readiness && !card.readiness.play_allowed ? (
          <ReadinessNotice readiness={card.readiness} onRetry={onRetried} className="mb-3 text-xs" />
        ) : null}
        <div className="mb-3 flex flex-wrap gap-1.5">
          {adventure.level_range ? <Pill>Levels {adventure.level_range}</Pill> : null}
          {isDev ? <Pill className="font-mono">{adventure.slug}</Pill> : null}
          {isDev && adventure.chunks != null ? <Pill>{adventure.chunks} chunks</Pill> : null}
          {isDev && card.campaign ? <Pill className="font-mono">campaign #{card.campaign.id}</Pill> : null}
        </div>
        {state === "recovery" ? <p className="mb-3 text-sm text-warning">{card.campaign ? "The bound adventure source needs recovery. Open the campaign to provide its PDF or choose homebrew play." : failureMessage || "This adventure source needs recovery. Inspect its preview and retry the retained source, or upload a corrected PDF."}</p> : null}
        {state === "failed" ? (
          <p className="mb-3 rounded-control bg-danger-soft px-2 py-1.5 text-xs text-danger">
            {failureMessage || (job?.kind === "generation"
              ? "Edgar couldn't finish generating this campaign. Resume the saved chapters or restart generation if retained work is unavailable."
              : "Edgar couldn't prepare this campaign. Retry the retained source or upload a corrected PDF.")}
          </p>
        ) : null}
        {adventure.extraction_preview_available ? <ExtractionPreview slug={adventure.slug} onUsePreview={canReviewCurrentPdf ? (sourceHash, extractionHash) => retry({ reviewed_source_sha256: sourceHash, reviewed_extraction_sha256: extractionHash }) : undefined} /> : null}
        {jobRunning ? (
          <div role="status" className="mb-3 rounded-control border border-info/30 bg-info-soft px-3 py-2 text-sm text-ink">
            <p className="font-medium">{progress ?? "Preparation is in progress."}</p>
            {job?.explanation ? <p className="mt-1 text-xs text-ink-muted">{job.explanation}</p> : null}
            {job.state === "cancel_requested" ? (
              <p className="mt-2 text-xs text-ink-muted">Cancellation requested…</p>
            ) : isPublicationCommitting(job) ? (
              <p className="mt-2 text-xs text-ink-muted">Finishing publication of the validated source. Cancellation is no longer available at this stage.</p>
            ) : (
              <Button variant="secondary" size="sm" loading={cancelling} onClick={() => void cancel()} className="mt-2">Cancel preparation</Button>
            )}
          </div>
        ) : null}
        {job && ["interrupted", "failed", "cancelled"].includes(job.state) ? (
          <div role="status" className="mb-3 rounded-control border border-warning/30 bg-warning-soft px-3 py-2 text-sm text-ink">
            <p className="font-medium">{jobFailureHeading(job)}</p>
            {job.failure ? <ErrorNotice error={job.failure} className="mt-2" /> : null}
            {job.explanation ? <p className="mt-1 text-xs text-ink-muted">{job.explanation}</p> : null}
          </div>
        ) : null}
        {canRetryJob ? <Button variant="secondary" size="sm" loading={retrying} onClick={() => void retry()} className="mb-3">{job?.action_label ?? "Retry retained source"}</Button> : null}
        {adventure.preparation_setup_required ? <PreparationRecovery slug={adventure.slug}
          edition={adventure.rules_edition} onStarted={onRetried} /> : null}
        <ErrorNotice error={retryError} />
        {state === "processing" && !job ? (
          <p className="mb-3 text-xs italic text-ink-muted">
            Preparation is in progress. You can leave this page; it keeps working.
          </p>
        ) : null}
        {state === "ended" ? (
          <p className="mb-3 text-xs text-ink-muted">
            This campaign has ended. Open it to review or reopen it.
          </p>
        ) : null}
        <div className="mt-auto flex gap-2">
          <Button
            className="flex-1"
            size="sm"
            variant={state === "ended" ? "secondary" : "primary"}
            disabled={!playable}
            loading={opening}
            loadingText="Opening…"
            onClick={() => onPlay(card)}
          >
            {card.campaign && card.readiness?.play_allowed === false ? (
              "View campaign"
            ) : state === "ready" ? (
              <>
                <PlayIcon className="h-3.5 w-3.5" /> Play
              </>
            ) : state === "ended" ? (
              "Open"
            ) : state === "recovery" ? (
              "Recover source"
            ) : state === "failed" ? (
              "Unavailable"
            ) : (
              "Preparing…"
            )}
          </Button>
          <Button
            variant="danger"
            size="sm"
            loading={deleting}
            loadingText="Deleting…"
            onClick={() => onDelete(card)}
            aria-label={`Delete ${name}`}
          >
            Delete
          </Button>
        </div>
      </div>
    </article>
  );
}
