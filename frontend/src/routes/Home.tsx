/** The reusable adventure catalogue. Campaign playthroughs have their own library. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { getMe, listAdventures } from "../api/client";
import type { AdventureRead } from "../api/types";
import { AdventureCover } from "../components/campaign/AdventureCover";
import { AdventureStatusBadge } from "../components/campaign/AdventurePreparation";
import { Button } from "../components/ui/Button";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { OfflineState } from "../components/ui/ErrorNotice";
import { Input } from "../components/ui/Field";
import { PlusIcon } from "../components/ui/icons";
import { Page, PageHeader, SectionTitle } from "../components/ui/Page";
import { isJobActive } from "../lib/adventureJob";
import { stripGenerationPlaceholder } from "../lib/campaign";
import { useQuery } from "../lib/useQuery";
import { CreatePanel } from "./home/CreatePanel";
import { WelcomeForm } from "./home/WelcomeForm";

export default function Home() {
  const me = useQuery("me", getMe);
  const adventures = useQuery(me.data ? "adventures" : null, listAdventures);
  const [createOpen, setCreateOpen] = useState<boolean | null>(null);
  const [search, setSearch] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const hasPending =
    adventures.data?.some(
      (a) => a.status === "processing" || isJobActive(a.job),
    ) ?? false;
  const { loading, refetch } = adventures;
  useEffect(() => {
    if (!hasPending || loading) return;
    const timer = window.setTimeout(refetch, 3000);
    return () => window.clearTimeout(timer);
  }, [hasPending, loading, refetch]);

  function created(adventure: AdventureRead) {
    adventures.setData((previous) => [
      adventure,
      ...(previous ?? []).filter((a) => a.slug !== adventure.slug),
    ]);
    setCreateOpen(false);
  }

  if (me.error && me.data === undefined)
    return (
      <Page>
        <OfflineState error={me.error} onRetry={me.refetch} />
      </Page>
    );
  if (me.data === undefined)
    return (
      <Page>
        <LoadingText />
      </Page>
    );
  if (me.data === null)
    return (
      <Page width="sm">
        <WelcomeForm onCreated={(user) => me.setData(() => user)} />
      </Page>
    );
  const visible = (adventures.data ?? []).filter(
    (a) => showArchived || !a.library_archived,
  );
  const filtered = visible.filter((a) =>
    `${a.title} ${a.description ?? ""}`
      .toLowerCase()
      .includes(search.trim().toLowerCase()),
  );
  const showCreate = createOpen ?? adventures.data?.length === 0;
  const archiveCount =
    adventures.data?.filter((a) => a.library_archived).length ?? 0;

  return (
    <Page>
      <PageHeader
        title="Adventures"
        eyebrow="THE ADVENTURE LIBRARY"
        description="Find your next story. Explore an adventure, choose your characters, and make it your own campaign."
        actions={
          <Button
            variant="secondary"
            aria-expanded={showCreate}
            aria-controls="new-adventure"
            onClick={() => setCreateOpen(!showCreate)}
          >
            <PlusIcon className="h-4 w-4" /> New adventure
          </Button>
        }
      />
      {showCreate ? (
        <section
          id="new-adventure"
          className="space-y-3"
          aria-labelledby="new-adventure-title"
        >
          <SectionTitle>
            <span id="new-adventure-title">New adventure</span>
          </SectionTitle>
          <CreatePanel onCreated={created} />
        </section>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Input
          aria-label="Search adventures"
          placeholder="Search adventures…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          className="sm:max-w-sm"
        />
        {archiveCount > 0 ? (
          <label className="inline-flex min-h-11 items-center gap-2 text-sm text-ink-muted">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(event) => setShowArchived(event.target.checked)}
            />{" "}
            Show archived ({archiveCount})
          </label>
        ) : null}
      </div>
      <section aria-label="Adventure catalogue">
        {adventures.error && adventures.data === undefined ? (
          <OfflineState error={adventures.error} onRetry={adventures.refetch} />
        ) : adventures.data === undefined ? (
          <LoadingText>Loading adventures…</LoadingText>
        ) : filtered.length === 0 ? (
          <EmptyState
            title={search ? "No matching adventures" : "No adventures yet"}
          >
            {search
              ? "Try another title or setting."
              : "Generate an adventure or upload a PDF to begin your library."}
          </EmptyState>
        ) : (
          <div className="grid auto-rows-fr gap-5 sm:grid-cols-2 lg:grid-cols-3">
            {filtered.map((adventure) => (
              <Link
                key={adventure.slug}
                to={`/adventures/${encodeURIComponent(adventure.slug)}`}
                className="group flex min-w-0 flex-col overflow-hidden rounded-card border border-line bg-surface-sunken transition-colors hover:border-ember-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400"
              >
                <article className="flex h-full flex-col">
                  <div className="[&>div]:h-44 [&>img]:h-44">
                    <AdventureCover
                      coverImage={adventure.cover_image}
                      name={adventure.title}
                    />
                  </div>
                  <div className="flex flex-1 flex-col gap-3 p-5">
                    <div className="flex flex-wrap gap-2">
                      <AdventureStatusBadge adventure={adventure} />
                      {adventure.library_archived ? (
                        <span className="text-xs text-ink-muted">Archived</span>
                      ) : null}
                    </div>
                    <h2 className="break-words font-display text-xl leading-tight group-hover:text-ember-200">
                      {stripGenerationPlaceholder(adventure.title)}
                    </h2>
                    <p className="line-clamp-3 text-sm leading-relaxed text-ink-muted">
                      {adventure.description ||
                        "A new story is waiting. Open this adventure to see its details and preparation status."}
                    </p>
                    <div className="mt-auto flex flex-wrap gap-x-3 gap-y-1 pt-2 text-xs text-ink-subtle">
                      <span>
                        {adventure.level_range
                          ? `Levels ${adventure.level_range}`
                          : "Levels not specified"}
                      </span>
                      <span>
                        D&amp;D 5e
                        {adventure.rules_edition &&
                        adventure.rules_edition !== "unspecified"
                          ? ` · ${adventure.rules_edition}`
                          : ""}
                      </span>
                    </div>
                    <span className="text-sm font-medium text-ember-300">
                      View adventure <span aria-hidden>→</span>
                    </span>
                  </div>
                </article>
              </Link>
            ))}
          </div>
        )}
      </section>
    </Page>
  );
}
