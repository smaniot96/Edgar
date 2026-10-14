import { useState } from "react";

import { retryAdventure } from "../../api/client";
import type { AdventureRead } from "../../api/types";
import { useMode } from "../../dev/ModeContext";
import type { CardModel } from "../../lib/campaignCards";
import { cn } from "../../lib/cn";
import { relativeTime } from "../../lib/format";
import { Pill } from "../Pill";
import { Button } from "../ui/Button";
import { ErrorNotice } from "../ui/ErrorNotice";
import { PlayIcon } from "../ui/icons";
import { cardClasses } from "../ui/styles";
import { ExtractionPreview } from "./ExtractionPreview";

function StatusBadge({ state }: { state: CardModel["state"] }) {
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
  const [retryError, setRetryError] = useState<unknown>(null);
  const { adventure, name, state, lastPlayed } = card;
  const playable = state === "ready" || state === "ended" || (state === "recovery" && !!card.campaign);
  const showAdventureTitle = name !== adventure.title;
  const recoverable = state === "failed" || state === "recovery";

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

  return (
    <article className={cn(cardClasses, "flex flex-col")}>
      <Cover adventure={adventure} name={name} />
      <div className="flex flex-1 flex-col p-4">
        <div className="mb-1 flex items-start justify-between gap-2">
          <h3 className="font-display text-display-sm leading-tight">{name}</h3>
          <StatusBadge state={state} />
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
        <div className="mb-3 flex flex-wrap gap-1.5">
          {adventure.level_range ? <Pill>Levels {adventure.level_range}</Pill> : null}
          {isDev ? <Pill className="font-mono">{adventure.slug}</Pill> : null}
          {isDev && adventure.chunks != null ? <Pill>{adventure.chunks} chunks</Pill> : null}
          {isDev && card.campaign ? <Pill className="font-mono">campaign #{card.campaign.id}</Pill> : null}
        </div>
        {state === "recovery" ? <p className="mb-3 text-sm text-warning">{card.campaign ? "The bound adventure source needs recovery. Open the campaign to provide its PDF or choose homebrew play." : adventure.error || "This adventure source needs recovery. Inspect its preview and retry the retained source, or upload a corrected PDF."}</p> : null}
        {state === "failed" ? (
          <p className="mb-3 rounded-control bg-danger-soft px-2 py-1.5 text-xs text-danger">
            {adventure.error || "Edgar couldn't prepare this campaign. Retry the retained source or upload a corrected PDF."}
          </p>
        ) : null}
        {adventure.extraction_preview_available ? <ExtractionPreview slug={adventure.slug} onUsePreview={recoverable && adventure.ingestion?.source_kind === "pdf" ? (sourceHash, extractionHash) => retry({ reviewed_source_sha256: sourceHash, reviewed_extraction_sha256: extractionHash }) : undefined} /> : null}
        {recoverable && adventure.can_retry ? <Button variant="secondary" size="sm" loading={retrying} onClick={() => void retry()} className="mb-3">Retry retained source</Button> : null}
        <ErrorNotice error={retryError} />
        {state === "processing" ? (
          <p className="mb-3 text-xs italic text-ink-muted">
            Building your campaign… larger sizes take a few minutes. You can leave this page; it
            keeps working.
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
            {state === "ready" ? (
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
