import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  ApiError,
  getAdventure,
  isNotFound,
  listCharacters,
  setAdventureLibraryArchived,
  startAdventureCampaign,
} from "../api/client";
import { AdventureCover } from "../components/campaign/AdventureCover";
import { LevelRecommendation } from "../components/campaign/LevelRecommendation";
import {
  AdventurePreparation,
  AdventureStatusBadge,
} from "../components/campaign/AdventurePreparation";
import { Button, ButtonLink } from "../components/ui/Button";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { ErrorNotice, OfflineState } from "../components/ui/ErrorNotice";
import { TextField } from "../components/ui/Field";
import { ArrowLeftIcon } from "../components/ui/icons";
import { Page } from "../components/ui/Page";
import { linkClasses } from "../components/ui/styles";
import { isJobActive } from "../lib/adventureJob";
import { recommendedLevels, stripGenerationPlaceholder } from "../lib/campaign";
import { useQuery } from "../lib/useQuery";

type CampaignIntent = {
  requestId: string;
  title: string;
  character_ids: number[];
};

export default function Adventure() {
  const { slug = "" } = useParams<{ slug: string }>();
  return <AdventureScreen key={slug} slug={slug} />;
}

function AdventureScreen({ slug }: { slug: string }) {
  const navigate = useNavigate();
  const adventure = useQuery(slug ? `adventure:${slug}` : null, (signal) =>
    getAdventure(slug, signal),
  );
  const characters = useQuery("characters", listCharacters);
  const [name, setName] = useState<string | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [starting, setStarting] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Keep the exact request on ambiguous failures and across reloads. Editing cannot
  // accidentally create a second campaign while the first request may have succeeded.
  const storageKey = `edgar:new-campaign:${slug}`;
  const [pending, setPending] = useState<CampaignIntent | null>(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || "null");
      return saved &&
        typeof saved.requestId === "string" &&
        typeof saved.title === "string" &&
        Array.isArray(saved.character_ids) &&
        saved.character_ids.length > 0 &&
        saved.character_ids.every(
          (id: unknown) => Number.isInteger(id) && Number(id) > 0,
        )
        ? saved
        : null;
    } catch {
      return null;
    }
  });
  const hasPending =
    adventure.data?.status === "processing" || isJobActive(adventure.data?.job);
  const { refetch, loading } = adventure;
  useEffect(() => {
    if (!hasPending || loading) return;
    const timer = window.setTimeout(refetch, 3000);
    return () => window.clearTimeout(timer);
  }, [hasPending, loading, refetch]);

  async function start() {
    const current = adventure.data;
    if (!current || starting) return;
    const intent = pending ?? {
      requestId: crypto.randomUUID(),
      title: (name ?? stripGenerationPlaceholder(current.title)).trim(),
      character_ids: selected,
    };
    if (!intent.title || intent.character_ids.length === 0) return;
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(intent));
    } catch {
      setError(
        new Error(
          "Allow browser storage so a retry can safely resume campaign creation.",
        ),
      );
      return;
    }
    setPending(intent);
    setStarting(true);
    setError(null);
    try {
      const campaign = await startAdventureCampaign(slug, intent.requestId, {
        title: intent.title,
        character_ids: intent.character_ids,
      });
      sessionStorage.removeItem(storageKey);
      navigate(`/campaigns/${campaign.id}`, {
        state: {
          notice:
            "Campaign created. Your party is ready for its first session.",
        },
      });
    } catch (cause) {
      setError(cause);
      // A classified HTTP rejection happens before creation. An ambiguous network/server
      // failure retains the request, even if those characters now appear occupied.
      if (
        cause instanceof ApiError &&
        [400, 404, 409, 422].includes(cause.status)
      ) {
        sessionStorage.removeItem(storageKey);
        setPending(null);
      }
      characters.refetch();
    } finally {
      setStarting(false);
    }
  }

  async function toggleArchive() {
    if (!adventure.data) return;
    setArchiving(true);
    setError(null);
    try {
      const updated = await setAdventureLibraryArchived(
        slug,
        !adventure.data.library_archived,
      );
      adventure.setData(() => updated);
      setNotice(
        updated.library_archived
          ? "Adventure archived. Existing campaigns and saved stories remain available in Campaigns."
          : "Adventure restored to your library.",
      );
    } catch (cause) {
      setError(cause);
    } finally {
      setArchiving(false);
    }
  }

  if (isNotFound(adventure.error))
    return (
      <Page>
        <EmptyState
          title="Adventure not found"
          action={<ButtonLink to="/adventures">Browse adventures</ButtonLink>}
        >
          It may no longer be available.
        </EmptyState>
      </Page>
    );
  if (adventure.error && !adventure.data)
    return (
      <Page>
        <OfflineState error={adventure.error} onRetry={refetch} />
      </Page>
    );
  if (!adventure.data)
    return (
      <Page>
        <LoadingText>Loading adventure…</LoadingText>
      </Page>
    );
  const a = adventure.data;
  const title = stripGenerationPlaceholder(a.title);
  const canStart = a.status === "ready" && a.readiness?.play_allowed === true;
  const selectedIds = pending?.character_ids ?? selected;
  const partyName = pending?.title ?? name ?? title;

  return (
    <Page>
      <Link
        to="/adventures"
        className={`inline-flex min-h-11 items-center gap-1 text-sm ${linkClasses}`}
      >
        <ArrowLeftIcon className="h-4 w-4" /> Adventures
      </Link>
      <div className="grid items-start gap-6 md:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="space-y-6">
          <div className="overflow-hidden rounded-card border border-line [&>div]:h-56 [&>img]:h-56 sm:[&>div]:h-72 sm:[&>img]:h-72">
            <AdventureCover coverImage={a.cover_image} name={title} />
          </div>
          <header className="space-y-3">
            <AdventureStatusBadge adventure={a} />
            <h1 className="break-words font-display text-display-md">
              {title}
            </h1>
            <p className="whitespace-pre-line text-base leading-relaxed text-ink-muted">
              {a.description ||
                "A story to discover together. Choose your characters and start a campaign to explore this adventure."}
            </p>
          </header>
          <section
            className="rounded-card border border-line bg-surface-sunken p-5"
            aria-labelledby="about-adventure"
          >
            <h2 id="about-adventure" className="font-display text-xl">
              About this adventure
            </h2>
            <dl className="mt-4 grid grid-cols-2 gap-4 text-sm">
              <div>
                <dt className="text-xs text-ink-subtle">Character levels</dt>
                <dd className="mt-1">{a.level_range || "Not specified"}</dd>
              </div>
              <div>
                <dt className="text-xs text-ink-subtle">Game system</dt>
                <dd className="mt-1">
                  D&amp;D 5e
                  {a.rules_edition && a.rules_edition !== "unspecified"
                    ? ` · ${a.rules_edition}`
                    : ""}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-ink-subtle">Party</dt>
                <dd className="mt-1">One or more characters</dd>
              </div>
              <div>
                <dt className="text-xs text-ink-subtle">Replayability</dt>
                <dd className="mt-1">A fresh story in every campaign</dd>
              </div>
            </dl>
            <p className="mt-4 border-t border-line pt-4 text-sm text-ink-muted">
              Each campaign has its own characters, sessions and outcomes. This
              adventure stays in your library to play again.
            </p>
          </section>
          <AdventurePreparation adventure={a} onUpdated={refetch} />
        </div>
        <aside className="space-y-4 md:sticky md:top-0">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void start();
            }}
            className="space-y-4 rounded-card border border-ember-700/60 bg-surface-sunken p-5"
          >
            <div>
              <h2 className="font-display text-xl">Start a campaign</h2>
              <p className="mt-1 text-sm text-ink-muted">
                Give your story a name and gather your party.
              </p>
            </div>
            <TextField
              label="Campaign name"
              value={partyName}
              required
              maxLength={255}
              disabled={starting || !!pending}
              onChange={(event) => setName(event.target.value)}
            />
            <fieldset
              disabled={starting || !!pending || !canStart}
              className="min-w-0 space-y-2"
            >
              <legend className="mb-2 text-sm font-medium">
                Choose characters
              </legend>
              {characters.error ? (
                <ErrorNotice
                  error={characters.error}
                  onRetry={characters.refetch}
                />
              ) : characters.data === undefined ? (
                <LoadingText>Loading characters…</LoadingText>
              ) : characters.data.length === 0 ? (
                <p className="text-sm text-ink-muted">
                  Create a character to begin.
                </p>
              ) : (
                <div className="max-h-80 space-y-2 overflow-y-auto">
                  {characters.data.map((character) => (
                    <label
                      key={character.id}
                      className={`flex min-h-14 items-start gap-3 rounded-control border p-3 text-sm ${selectedIds.includes(character.id) ? "border-ember-600 bg-ember-950/30" : "border-line"} ${character.current_assignment && !selectedIds.includes(character.id) ? "opacity-60" : ""}`}
                    >
                      <input
                        type="checkbox"
                        className="mt-1 shrink-0 accent-orange-500"
                        checked={selectedIds.includes(character.id)}
                        disabled={!!character.current_assignment}
                        onChange={(event) =>
                          setSelected((previous) =>
                            event.target.checked
                              ? [...previous, character.id]
                              : previous.filter((id) => id !== character.id),
                          )
                        }
                      />
                      <span className="min-w-0">
                        <span className="block break-words font-medium">
                          {character.name}
                        </span>
                        <span className="block text-xs text-ink-muted">
                          Level {character.level} {character.character_class}
                        </span>
                        {character.current_assignment ? (
                          <span className="mt-1 block break-words text-xs text-warning">
                            In {character.current_assignment.campaign_title}
                          </span>
                        ) : null}
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </fieldset>
            <LevelRecommendation range={a.level_range} heroes={characters.data?.filter((character) => selectedIds.includes(character.id))} />
            <Link
              to={`/characters?level=${Math.min(5, recommendedLevels(a.level_range)?.min ?? 1)}`}
              className={`inline-flex min-h-11 items-center text-sm ${linkClasses}`}
            >
              Create or manage characters
            </Link>
            {!canStart ? (
              <p className="text-sm text-warning">
                This adventure must be Ready before you can start a campaign.
              </p>
            ) : (
              <p className="text-xs text-ink-muted">
                {selectedIds.length
                  ? `${selectedIds.length} character${selectedIds.length === 1 ? "" : "s"} selected`
                  : "Select at least one character."}
              </p>
            )}
            {pending ? (
              <p role="status" className="text-xs text-ink-muted">
                Your creation request is saved. Retry safely resumes the same
                campaign.
              </p>
            ) : null}
            <ErrorNotice error={error} />
            <Button
              type="submit"
              className="w-full"
              loading={starting}
              loadingText="Creating campaign…"
              disabled={
                (!canStart && !pending) ||
                selectedIds.length === 0 ||
                !partyName.trim()
              }
            >
              {pending ? "Retry campaign creation" : "Create campaign"}
            </Button>
          </form>
          <Button
            variant="ghost"
            className="w-full"
            loading={archiving}
            onClick={() => void toggleArchive()}
          >
            {a.library_archived
              ? "Restore to adventure library"
              : "Archive adventure"}
          </Button>
          {notice ? (
            <p role="status" className="text-sm text-ink-muted">
              {notice}
            </p>
          ) : null}
        </aside>
      </div>
    </Page>
  );
}
