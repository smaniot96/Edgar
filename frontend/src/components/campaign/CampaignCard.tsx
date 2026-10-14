import type { AdventureRead } from "../../api/types";
import { useMode } from "../../dev/ModeContext";
import type { CardModel } from "../../lib/campaignCards";
import { cn } from "../../lib/cn";
import { relativeTime } from "../../lib/format";
import { Pill } from "../Pill";
import { Button } from "../ui/Button";
import { PlayIcon } from "../ui/icons";
import { cardClasses } from "../ui/styles";

function StatusBadge({ state }: { state: CardModel["state"] }) {
  switch (state) {
    case "ready":
      return <Pill tone="success">Ready</Pill>;
    case "ended":
      return <Pill tone="neutral">Ended</Pill>;
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
}: {
  card: CardModel;
  onPlay: (card: CardModel) => void;
  onDelete: (card: CardModel) => void;
  opening: boolean;
  deleting: boolean;
}) {
  const { isDev } = useMode();
  const { adventure, name, state, lastPlayed } = card;
  const playable = state === "ready" || state === "ended";
  const showAdventureTitle = name !== adventure.title;

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
        {state === "failed" ? (
          <p className="mb-3 rounded-control bg-danger-soft px-2 py-1.5 text-xs text-danger">
            Edgar couldn't prepare this campaign. Delete it and try again.
            {isDev && adventure.error ? (
              <span className="mt-1 block font-mono text-[11px] text-ink-muted">{adventure.error}</span>
            ) : null}
          </p>
        ) : null}
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
