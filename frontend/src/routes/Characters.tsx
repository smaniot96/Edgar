/**
 * Characters (PG) library: every character with class/level/HP and the campaign it's engaged in
 * (or "Available"), plus the shared CharacterForm and delete.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { deleteCharacter, listCharacters } from "../api/client";
import type { CharacterListItem } from "../api/types";
import { CharacterForm } from "../components/CharacterForm";
import { Pill } from "../components/Pill";
import { Button } from "../components/ui/Button";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { ErrorNotice, OfflineState } from "../components/ui/ErrorNotice";
import { Page, PageHeader, SectionTitle } from "../components/ui/Page";
import { cardClasses } from "../components/ui/styles";
import { useMode } from "../dev/ModeContext";
import { cn } from "../lib/cn";
import { useQuery } from "../lib/useQuery";

export default function Characters() {
  const { isDev } = useMode();
  const chars = useQuery("characters", (signal) => listCharacters(signal));
  const [actionError, setActionError] = useState<unknown>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  async function handleDelete(c: CharacterListItem) {
    if (!window.confirm(`Delete character "${c.name}"? This cannot be undone.`)) return;
    setActionError(null);
    setDeletingId(c.id);
    try {
      await deleteCharacter(c.id);
      chars.refetch();
    } catch (e) {
      setActionError(e);
    } finally {
      setDeletingId(null);
    }
  }

  if (chars.error && chars.data === undefined) {
    return (
      <Page width="md">
        <OfflineState error={chars.error} onRetry={chars.refetch} />
      </Page>
    );
  }

  return (
    <Page width="md">
      <PageHeader
        title="Characters"
        description="Your player characters. A character can be engaged in one campaign at a time."
      />

      <CharacterForm onCreated={() => chars.refetch()} />

      <ErrorNotice error={actionError} />

      <section aria-labelledby="your-characters" className="space-y-3">
        <SectionTitle>
          <span id="your-characters">Your characters</span>
        </SectionTitle>
        {chars.data === undefined ? (
          <LoadingText />
        ) : chars.data.length === 0 ? (
          <EmptyState title="No characters yet">
            Pick a preset above and give them a name. You'll choose who to play when you start a
            session — you can also create one right from a campaign.
          </EmptyState>
        ) : (
          <ul className="space-y-2">
            {chars.data.map((c) => (
              <li
                key={c.id}
                className={cn(cardClasses, "flex items-center justify-between gap-3 px-4 py-3")}
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">
                    {c.name}
                    {isDev ? (
                      <span className="ml-2 font-mono text-xs text-ink-subtle">#{c.id}</span>
                    ) : null}
                  </p>
                  <p className="text-xs text-ink-muted">
                    Level {c.level} {c.character_class} · HP {c.hp_max}
                  </p>
                  <div className="mt-1.5">
                    {c.current_assignment ? (
                      <Link
                        to={`/campaigns/${c.current_assignment.campaign_id}`}
                        className="inline-block rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400"
                      >
                        <Pill tone="ember" className="hover:underline">
                          In: {c.current_assignment.campaign_title}
                        </Pill>
                      </Link>
                    ) : (
                      <Pill tone="success">Available</Pill>
                    )}
                  </div>
                </div>
                <Button
                  variant="danger"
                  size="sm"
                  className="shrink-0"
                  loading={deletingId === c.id}
                  loadingText="Deleting…"
                  onClick={() => void handleDelete(c)}
                  aria-label={`Delete ${c.name}`}
                >
                  Delete
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </Page>
  );
}
