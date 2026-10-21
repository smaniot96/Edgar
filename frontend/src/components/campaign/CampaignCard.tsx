import { useEffect, useId, useRef, useState, type ReactNode } from "react";

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

function StatusBadge({ state, readiness, homebrewWithoutSource, jobState }: { state: CardModel["state"]; readiness: ReadinessRead | null; homebrewWithoutSource: boolean; jobState?: string }) {
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
      // An interrupted or cancelled run keeps its saved work and can resume; only a real failure is red.
      if (jobState === "interrupted") return <Pill tone="warning">Interrupted</Pill>;
      if (jobState === "cancelled") return <Pill tone="neutral">Cancelled</Pill>;
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

/**
 * The card's secondary actions. It opens upward from the footer so it stays inside the card, closes
 * on an outside press or Escape, and hands focus back to its button. The items stay mounted
 * (hidden) so they are never missing from the document.
 */
function CardMenu({ className, children }: { className?: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  function close() {
    setOpen(false);
    trigger.current?.focus();
  }

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={root} className={cn("relative", className)}>
      <button
        ref={trigger}
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
        className="inline-flex min-h-11 items-center rounded-control border border-line px-3 text-sm text-ink-muted hover:bg-surface-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400"
      >
        More
      </button>
      {/* Choosing an item closes the menu and parks focus on its button, which is what a dialog opened by the item returns to. */}
      <div
        id={panelId}
        hidden={!open}
        onClick={(event) => { if ((event.target as HTMLElement).closest("button")) close(); }}
        className="absolute bottom-full right-0 z-10 mb-1 w-max rounded-control border border-line bg-surface-raised p-2 shadow-lg"
      >
        {children}
      </div>
    </div>
  );
}

export function CampaignCard({
  card,
  onPlay,
  onDelete,
  opening,
  onRetried,
}: {
  card: CardModel;
  onPlay: (card: CardModel) => void;
  onDelete: (card: CardModel) => void;
  opening: boolean;
  onRetried?: () => void;
}) {
  const { isDev } = useMode();
  const [retrying, setRetrying] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [retryError, setRetryError] = useState<unknown>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const { adventure, name, state, lastPlayed, primarySession } = card;
  const job = adventure.job;
  const jobRunning = job != null && ["queued", "running", "cancel_requested"].includes(job.state);
  const jobFailureShown = job != null && ["interrupted", "failed", "cancelled"].includes(job.state);
  // A failed run explains itself (and offers its retry); the generic readiness gate would repeat it.
  const showReadiness = !jobFailureShown &&
    ((state === "ready" && card.readiness?.play_allowed !== true) || card.readiness?.play_allowed === false);
  const adventureSubtitle = adventureTitleForDisplay(adventure);
  const showAdventureTitle = adventureSubtitle != null && adventureSubtitle !== name;
  const homebrewWithoutSource = card.campaign?.source_mode === "homebrew" &&
    (adventure.slug.startsWith("homebrew_") || adventure.status !== "ready");
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

  const needsSetup = !!adventure.preparation_setup_required;
  // The label of what opening this card does, or null when there is nothing to open yet. A card
  // that cannot be opened offers its one real next step (set up, retry) as the primary button
  // instead of a disabled one.
  const playLabel: ReactNode | null =
    state === "ended" ? "Open"
    : primarySession?.readiness?.play_allowed === true ? <><PlayIcon className="h-3.5 w-3.5" /> Continue</>
    : primarySession?.has_history && primarySession.readiness?.history_allowed === true ? "Read story"
    : card.campaign && state === "ready" && card.readiness?.play_allowed !== true ? "View campaign"
    : !card.campaign && state === "ready" && card.readiness?.play_allowed !== true ? null
    : state === "ready" && card.campaign ? "Set up first session"
    : state === "ready" ? <><PlayIcon className="h-3.5 w-3.5" /> Start adventure</>
    : state === "recovery" ? "Recover source"
    : null;
  const longName = name.length > 48;
  const retryLabel = job?.action_label ?? "Retry retained source";
  const retryAsPrimary = playLabel == null && !needsSetup && canRetryJob;

  const progress = preparationProgress(job);
  const failure = job?.failure ?? (adventure.failure_code ? { code: adventure.failure_code } : null);
  const failureMessage = failure ? safeTurnError(failure).message : null;

  return (
    <article className={cn(cardClasses, "flex min-w-0 flex-col")}>
      <div className="[&>div]:h-20 [&>img]:h-20 sm:[&>div]:h-24 sm:[&>img]:h-24 [&>div>span]:text-5xl">
        <AdventureCover coverImage={adventure.cover_image} name={name} />
      </div>
      <div className="flex flex-1 flex-col p-4">
        {/* The status always sits above the title, so every card's title starts at the same place
            whatever its length. */}
        <div className="mb-2"><StatusBadge state={state} readiness={card.readiness} homebrewWithoutSource={homebrewWithoutSource} jobState={job?.state} /></div>
        <h3 className={cn("mb-1 min-w-0 max-w-full break-words [overflow-wrap:anywhere] font-display leading-tight", longName ? "text-lg" : "text-xl")}>{name}</h3>
        {showAdventureTitle ? (
          <p className="mb-1 text-xs text-ink-muted [overflow-wrap:anywhere]">{adventureSubtitle}</p>
        ) : null}
        {/* The save is one block: who is playing and when, then where the story stands. The time is
            stated once; without a save, one line says why there is nothing to continue. */}
        {primarySession ? (
          <div className="mb-3 mt-1 space-y-1.5 border-l-2 border-ember-700/60 pl-3">
            <p className="break-words text-sm [overflow-wrap:anywhere]">
              <span className="font-medium text-ink">{primarySession.character_name || "No hero"}</span>
              <span className="text-ink-muted">
                {primarySession.has_history && primarySession.last_played_at
                  ? ` · last played ${relativeTime(primarySession.last_played_at)}`
                  : " · no story history yet"}
              </span>
            </p>
            <SavedPlaythroughContext session={primarySession} className="" />
          </div>
        ) : (
          <p className="mb-2 text-xs text-ink-subtle">
            {lastPlayed ? `Last played ${relativeTime(lastPlayed)}` : card.campaign ? "No saved playthrough yet" : "Not played yet"}
          </p>
        )}
        {adventure.description ? (
          <p className="mb-2 line-clamp-2 text-sm text-ink-muted">{adventure.description}</p>
        ) : null}
        {showReadiness ? (
          <ReadinessNotice readiness={card.readiness} onRetry={onRetried} announce={false} className="mb-3 text-xs" />
        ) : null}
        {adventure.level_range ? (
          <p className="mb-3 text-xs text-ink-subtle [overflow-wrap:anywhere]">
            <span className="font-medium">Levels</span> {adventure.level_range}
          </p>
        ) : null}
        <div className="mb-3 flex flex-wrap gap-1.5 empty:hidden">
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
              <Button variant="secondary" size="sm" loading={cancelling} onClick={() => void cancel()} className="mt-2 min-h-11">Cancel preparation</Button>
            )}
          </div>
        ) : null}
        {jobFailureShown ? (
          <div role="status" className="mb-3 rounded-control border border-warning/30 bg-warning-soft px-3 py-2 text-sm text-ink">
            <p className="font-medium">{jobFailureHeading(job)}</p>
            {/* Inside this status region already: no second box and no second announcement. */}
            {job.failure ? <ErrorNotice error={job.failure} bare announce={false} className="mt-2" /> : null}
            {job.explanation ? <p className="mt-1 text-xs text-ink-muted">{job.explanation}</p> : null}
          </div>
        ) : null}
        {canRetryJob && !retryAsPrimary && !needsSetup ? <Button variant="secondary" size="sm" loading={retrying} onClick={() => void retry()} className="mb-3 min-h-11">{retryLabel}</Button> : null}
        {needsSetup ? <p className="mb-3 text-sm text-ink-muted">Complete your setup to start private preparation.</p> : null}
        <ErrorNotice error={retryError} />
        {state === "processing" && !job && !needsSetup ? (
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
          {needsSetup ? (
            <Button className="min-h-11 min-w-0 flex-1 whitespace-normal break-words" size="sm" onClick={() => setSetupOpen(true)}>
              Set up preparation
            </Button>
          ) : retryAsPrimary ? (
            <Button className="min-h-11 min-w-0 flex-1 whitespace-normal break-words" size="sm" loading={retrying} onClick={() => void retry()}>
              {retryLabel}
            </Button>
          ) : playLabel != null ? (
            <Button
              className="min-h-11 min-w-0 flex-1 whitespace-normal break-words"
              size="sm"
              variant={state === "ended" ? "secondary" : "primary"}
              loading={opening}
              loadingText="Opening…"
              onClick={() => onPlay(card)}
            >
              {playLabel}
            </Button>
          ) : null}
          <CardMenu className={needsSetup || retryAsPrimary || playLabel != null ? "" : "ml-auto"}>
            <Button
              variant="danger"
              size="sm"
              onClick={() => onDelete(card)}
              aria-label={`Delete ${name}`}
              aria-haspopup="dialog"
            >
              {card.campaign ? "Delete campaign" : "Delete adventure"}
            </Button>
          </CardMenu>
        </div>
        {needsSetup ? <PreparationRecovery open={setupOpen} slug={adventure.slug} edition={adventure.rules_edition}
          onClose={() => setSetupOpen(false)} onStarted={onRetried} /> : null}
      </div>
    </article>
  );
}
