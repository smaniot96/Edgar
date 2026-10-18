import { useState } from "react";

import { cancelAdventure, retryAdventure } from "../../api/client";
import type { ReadinessRead } from "../../api/types";
import { safeTurnError } from "../../api/turnStream";
import { AdventureCover } from "./AdventureCover";
import { SavedPlaythroughContext } from "./SavedPlaythroughContext";
import { ReadinessNotice } from "./ReadinessNotice";
import { useMode } from "../../dev/ModeContext";
import { adventureTitleForDisplay } from "../../lib/campaign";
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

function StatusBadge({ state, readiness, homebrewWithoutSource }: { state: CardModel["state"]; readiness: ReadinessRead | null; homebrewWithoutSource: boolean }) {
  if (state === "ready" && readiness?.play_allowed !== true) {
    return <Pill tone="warning">{readiness ? "Needs attention" : "Readiness unknown"}</Pill>;
  }
  // Homebrew play has no adventure source behind it, so "Ready" would promise module content.
  if (state === "ready" && homebrewWithoutSource) return <Pill tone="neutral">Homebrew</Pill>;
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
  const { adventure, name, state, lastPlayed, primarySession } = card;
  const job = adventure.job;
  const jobRunning = job != null && ["queued", "running", "cancel_requested"].includes(job.state);
  const playable = !!card.campaign || (state === "ready" && card.readiness?.play_allowed === true) || state === "ended";
  const showReadiness = (state === "ready" && card.readiness?.play_allowed !== true) || card.readiness?.play_allowed === false;
  const adventureSubtitle = adventureTitleForDisplay(adventure);
  const showAdventureTitle = adventureSubtitle != null && adventureSubtitle !== name;
  const homebrewWithoutSource = card.campaign?.source_mode === "homebrew" &&
    (adventure.slug.startsWith("homebrew_") || adventure.status !== "ready");
  const recoverable = state === "failed" || state === "recovery";
  const jobFailureShown = job != null && ["interrupted", "failed", "cancelled"].includes(job.state);
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
    <article className={cn(cardClasses, "flex min-w-0 flex-col")}>
      <div className="[&>div]:h-20 [&>img]:h-20 sm:[&>div]:h-24 sm:[&>img]:h-24 [&>div>span]:text-5xl">
        <AdventureCover coverImage={adventure.cover_image} name={name} />
      </div>
      <div className="flex flex-1 flex-col p-4">
        <div className="mb-1 flex min-w-0 items-start justify-between gap-2">
          <h3 className="min-w-0 line-clamp-3 break-words [overflow-wrap:anywhere] font-display text-xl leading-tight">{name}</h3>
          <StatusBadge state={state} readiness={card.readiness} homebrewWithoutSource={homebrewWithoutSource} />
        </div>
        {name.length > 80 ? (
          <details className="mb-1 text-sm">
            <summary className="inline-flex min-h-11 cursor-pointer items-center text-ink-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400">Full campaign title</summary>
            <p className="break-words [overflow-wrap:anywhere] text-ink">{name}</p>
          </details>
        ) : null}
        {showAdventureTitle ? (
          <p className="mb-1 text-xs text-ink-muted [overflow-wrap:anywhere]">{adventureSubtitle}</p>
        ) : null}
        <p className="mb-2 text-xs text-ink-subtle">
          {lastPlayed ? `Last played ${relativeTime(lastPlayed)}` : "Not played yet"}
        </p>
        {primarySession ? (
          <p className="mb-2 break-words text-sm text-ink-muted [overflow-wrap:anywhere]">
            {primarySession.character_name || "No hero"}
            {primarySession.has_history && primarySession.last_played_at
              ? ` · saved ${relativeTime(primarySession.last_played_at)}`
              : " · no story history yet"}
          </p>
        ) : card.campaign ? (
          <p className="mb-2 text-sm text-ink-muted">No saved playthrough yet</p>
        ) : null}
        <SavedPlaythroughContext session={primarySession} />
        {adventure.description ? (
          <p className="mb-2 line-clamp-2 text-sm text-ink-muted">{adventure.description}</p>
        ) : null}
        {showReadiness ? (
          <ReadinessNotice readiness={card.readiness} onRetry={onRetried} className="mb-3 text-xs" />
        ) : null}
        <div className="mb-3 flex flex-wrap gap-1.5">
          {adventure.level_range ? <Pill>Levels {adventure.level_range}</Pill> : null}
          {isDev ? <Pill className="font-mono">{adventure.slug}</Pill> : null}
          {isDev && adventure.chunks != null ? <Pill>{adventure.chunks} chunks</Pill> : null}
          {isDev && card.campaign ? <Pill className="font-mono">campaign #{card.campaign.id}</Pill> : null}
        </div>
        {/* One message per card: the readiness notice, the job failure block, or this fallback. */}
        {state === "recovery" && !showReadiness && !jobFailureShown ? <p className="mb-3 text-sm text-warning">{card.campaign ? "The bound adventure source needs recovery. Open the campaign to provide its PDF or choose homebrew play." : failureMessage || "This adventure source needs recovery. Inspect its preview and retry the retained source, or upload a corrected PDF."}</p> : null}
        {state === "failed" && !showReadiness && !jobFailureShown ? (
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
        {jobFailureShown ? (
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
        <div className="mt-auto flex flex-wrap gap-2">
          <Button
            className="min-h-11 min-w-0 flex-1 whitespace-normal break-words"
            size="sm"
            variant={state === "ended" ? "secondary" : "primary"}
            disabled={!playable}
            loading={opening}
            loadingText="Opening…"
            onClick={() => onPlay(card)}
          >
            {primarySession?.readiness?.play_allowed === true ? (
              <><PlayIcon className="h-3.5 w-3.5" /> Continue</>
            ) : primarySession?.has_history && primarySession.readiness?.history_allowed === true ? (
              "Read story"
            ) : card.campaign && state === "ready" && card.readiness?.play_allowed !== true ? (
              "View campaign"
            ) : !card.campaign && state === "ready" && card.readiness?.play_allowed !== true ? (
              card.readiness ? "Needs attention" : "Readiness unknown"
            ) : state === "ready" && card.campaign ? (
              "Set up first session"
            ) : state === "ready" ? (
              <>
                <PlayIcon className="h-3.5 w-3.5" /> Start adventure
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
          <details className="relative">
            <summary className="inline-flex min-h-11 cursor-pointer list-none items-center rounded-control border border-line px-3 text-sm text-ink-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400">More</summary>
            <div className="absolute right-0 z-10 mt-1 rounded-control border border-line bg-surface-raised p-2 shadow-lg">
              <Button
                variant="danger"
                size="sm"
                loading={deleting}
                loadingText="Deleting…"
                onClick={() => onDelete(card)}
                aria-label={`Delete ${name}`}
              >
                Delete campaign
              </Button>
            </div>
          </details>
        </div>
      </div>
    </article>
  );
}
