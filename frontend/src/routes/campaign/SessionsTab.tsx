import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  assignCharacterToCampaign,
  createSession,
  deleteSession,
  listCharacters,
  listSessions,
} from "../../api/client";
import type { CharacterListItem, SessionRead } from "../../api/types";
import { CharacterForm } from "../../components/CharacterForm";
import { Pill } from "../../components/Pill";
import { Button } from "../../components/ui/Button";
import { EmptyState, LoadingText } from "../../components/ui/EmptyState";
import { ErrorNotice } from "../../components/ui/ErrorNotice";
import { PlayIcon, PlusIcon } from "../../components/ui/icons";
import { cardClasses, linkClasses } from "../../components/ui/styles";
import { useMode } from "../../dev/ModeContext";
import { cn } from "../../lib/cn";
import { dateTime, relativeTime } from "../../lib/format";
import { rememberLastSession } from "../../lib/lastSession";
import { useQuery } from "../../lib/useQuery";

interface NumberedSession extends SessionRead {
  number: number;
}

export default function SessionsTab({
  campaignId,
  ended,
}: {
  campaignId: number;
  ended: boolean;
}) {
  const navigate = useNavigate();
  const { isDev } = useMode();
  const sessions = useQuery(`sessions:${campaignId}`, (s) => listSessions(campaignId, s));
  const characters = useQuery("characters", (s) => listCharacters(s));

  // `null` = default: open the picker when there are no sessions yet.
  const [pickerOpen, setPickerOpen] = useState<boolean | null>(null);
  const [creatingChar, setCreatingChar] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Number sessions oldest→newest ("Session 1", "Session 2"…), list newest first.
  const numbered = useMemo<NumberedSession[] | undefined>(() => {
    if (!sessions.data) return undefined;
    const asc = [...sessions.data].sort((a, b) => a.started_at.localeCompare(b.started_at));
    return asc.map((s, i) => ({ ...s, number: i + 1 })).reverse();
  }, [sessions.data]);

  const charById = useMemo(
    () => new Map((characters.data ?? []).map((c) => [c.id, c])),
    [characters.data],
  );

  const showPicker = !ended && (pickerOpen ?? numbered?.length === 0);

  function play(id: number) {
    rememberLastSession(id);
    navigate(`/play/${id}`);
  }

  async function startSession(character?: Pick<CharacterListItem, "id" | "current_assignment">) {
    setBusy(true);
    setError(null);
    try {
      // A PG must be assigned to this campaign before the first turn (the DM needs its sheet).
      if (character && character.current_assignment?.campaign_id !== campaignId) {
        await assignCharacterToCampaign(campaignId, character.id);
      }
      const s = await createSession(campaignId, character?.id);
      play(s.id);
    } catch (e) {
      // 409 → PG engaged in another campaign; the API's message says so.
      setError(e);
      setBusy(false);
      characters.refetch();
    }
  }

  async function handleDelete(s: NumberedSession) {
    if (!window.confirm(`Delete Session ${s.number}? Its history will be lost.`)) return;
    setError(null);
    try {
      await deleteSession(s.id);
      sessions.refetch();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-xl text-sm text-ink-muted">
          A session is one saved playthrough. Pick up where you left off with{" "}
          <strong className="text-ink">Continue</strong>, or start a fresh one.
        </p>
        {!showPicker && !ended ? (
          <Button
            onClick={() => {
              setError(null);
              setPickerOpen(true);
            }}
          >
            <PlusIcon className="h-4 w-4" /> New session
          </Button>
        ) : null}
      </div>

      {ended ? (
        <p className="rounded-control border border-line bg-surface-sunken px-3 py-2 text-sm text-ink-muted">
          This campaign has ended, so new sessions can't be started. Reopen it to keep playing.
        </p>
      ) : null}

      {showPicker ? (
        <section
          aria-labelledby="new-session-title"
          className={cn(cardClasses, "space-y-3 p-4")}
        >
          <div className="flex items-center justify-between">
            <h3 id="new-session-title" className="text-sm font-semibold">
              Start a new session
            </h3>
            {numbered && numbered.length > 0 ? (
              <Button variant="ghost" size="sm" onClick={() => setPickerOpen(false)}>
                Cancel
              </Button>
            ) : null}
          </div>
          <p className="text-sm text-ink-muted">
            Choose who you'll play — they join this campaign — or jump in without a character and
            let the DM improvise.
          </p>

          {characters.error ? (
            <ErrorNotice error={characters.error} onRetry={characters.refetch} />
          ) : characters.data === undefined ? (
            <LoadingText>Loading characters…</LoadingText>
          ) : characters.data.length === 0 && !creatingChar ? (
            <p className="text-sm text-ink-muted">You don't have any characters yet.</p>
          ) : (
            <div className="grid gap-2 sm:grid-cols-2">
              {characters.data.map((c) => {
                const elsewhere =
                  c.current_assignment != null && c.current_assignment.campaign_id !== campaignId;
                return (
                  <button
                    key={c.id}
                    type="button"
                    disabled={busy || elsewhere}
                    onClick={() => void startSession(c)}
                    className={cn(
                      "rounded-card border border-line bg-surface-sunken p-3 text-left transition-colors",
                      "hover:border-ember-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400",
                      "disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:border-line",
                    )}
                  >
                    <p className="text-sm font-medium">{c.name}</p>
                    <p className="text-xs text-ink-muted">
                      Level {c.level} {c.character_class} · HP {c.hp_max}
                    </p>
                    {elsewhere ? (
                      <p className="mt-1 text-xs text-warning">
                        Busy in {c.current_assignment?.campaign_title}
                      </p>
                    ) : null}
                  </button>
                );
              })}
            </div>
          )}

          {creatingChar ? (
            <CharacterForm
              title="New character"
              submitLabel="Create & start playing"
              onCancel={() => setCreatingChar(false)}
              onCreated={(c) => {
                setCreatingChar(false);
                characters.refetch();
                void startSession({ id: c.id, current_assignment: null });
              }}
              className="bg-surface-sunken"
            />
          ) : (
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" size="sm" disabled={busy} onClick={() => setCreatingChar(true)}>
                <PlusIcon className="h-3.5 w-3.5" /> Create a new character
              </Button>
              <Button
                variant="ghost"
                size="sm"
                loading={busy}
                loadingText="Starting…"
                onClick={() => void startSession()}
              >
                Start without a character
              </Button>
            </div>
          )}
        </section>
      ) : null}

      <ErrorNotice error={error} />

      <section aria-labelledby="sessions-title" className="space-y-2">
        <h3 id="sessions-title" className="text-sm font-semibold">
          Saved sessions
        </h3>
        {sessions.error && numbered === undefined ? (
          <ErrorNotice error={sessions.error} onRetry={sessions.refetch} />
        ) : numbered === undefined ? (
          <LoadingText />
        ) : numbered.length === 0 ? (
          <EmptyState title="No sessions yet">
            {ended
              ? "This campaign ended before any session was played."
              : "Choose a character above to begin your first session."}
          </EmptyState>
        ) : (
          <ul className="space-y-2">
            {numbered.map((s) => {
              const ch = s.active_character_id != null ? charById.get(s.active_character_id) : null;
              const who =
                s.active_character_id == null
                  ? "No character"
                  : ch
                    ? `${ch.name}, ${ch.character_class}`
                    : "Character removed";
              return (
                <li
                  key={s.id}
                  className={cn(
                    cardClasses,
                    "flex flex-wrap items-center justify-between gap-3 px-4 py-3",
                  )}
                >
                  <div className="min-w-0">
                    <p className="flex items-center gap-2 text-sm font-medium">
                      Session {s.number}
                      {s.ended_at ? <Pill>Finished</Pill> : null}
                    </p>
                    <p className="text-xs text-ink-muted">
                      {who} · started{" "}
                      <time dateTime={s.started_at} title={dateTime(s.started_at)}>
                        {relativeTime(s.started_at)}
                      </time>
                    </p>
                    {isDev ? (
                      <p className="font-mono text-[11px] text-ink-subtle">
                        session #{s.id}
                        {s.active_character_id != null ? ` · char #${s.active_character_id}` : ""}
                        {s.current_scene_id ? ` · scene ${s.current_scene_id}` : ""}
                      </p>
                    ) : null}
                  </div>
                  <div className="flex gap-2">
                    <Button size="sm" onClick={() => play(s.id)}>
                      <PlayIcon className="h-3 w-3" /> Continue
                    </Button>
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => void handleDelete(s)}
                      aria-label={`Delete Session ${s.number}`}
                    >
                      Delete
                    </Button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <p className="text-xs text-ink-subtle">
        Manage your roster on the{" "}
        <Link to="/characters" className={linkClasses}>
          Characters
        </Link>{" "}
        page.
      </p>
    </div>
  );
}
