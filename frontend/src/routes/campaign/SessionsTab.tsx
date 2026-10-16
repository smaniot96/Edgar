import { useMemo, useState } from "react";

import { deleteSession, listSessions } from "../../api/client";
import type { ReadinessRead, SessionRead } from "../../api/types";
import { ReadinessNotice } from "../../components/campaign/ReadinessNotice";
import { Pill } from "../../components/Pill";
import { Button, ButtonLink } from "../../components/ui/Button";
import { EmptyState, LoadingText } from "../../components/ui/EmptyState";
import { ErrorNotice } from "../../components/ui/ErrorNotice";
import { PlayIcon, PlusIcon } from "../../components/ui/icons";
import { cardClasses } from "../../components/ui/styles";
import { useMode } from "../../dev/ModeContext";
import { cn } from "../../lib/cn";
import { dateTime, relativeTime } from "../../lib/format";
import { rememberLastSession } from "../../lib/lastSession";
import { useQuery } from "../../lib/useQuery";
import { Link, useNavigate } from "react-router-dom";

interface NumberedSession extends SessionRead {
  number: number;
}

export default function SessionsTab({
  campaignId,
  ended,
  playAllowed = true,
  readiness,
}: {
  campaignId: number;
  ended: boolean;
  playAllowed?: boolean;
  readiness?: ReadinessRead | null;
}) {
  const navigate = useNavigate();
  const { isDev } = useMode();
  const sessions = useQuery(`sessions:${campaignId}`, (s) => listSessions(campaignId, s));
  const [error, setError] = useState<unknown>(null);

  const numbered = useMemo<NumberedSession[] | undefined>(() => {
    if (!sessions.data) return undefined;
    const asc = [...sessions.data].sort((a, b) => a.started_at.localeCompare(b.started_at));
    return asc.map((session, i) => ({ ...session, number: i + 1 })).reverse();
  }, [sessions.data]);

  const campaignCanPlay = readiness?.play_allowed ?? playAllowed;
  const canStart = !ended && campaignCanPlay;

  function play(session: SessionRead) {
    rememberLastSession(session.id);
    navigate(`/play/${session.id}`);
  }

  async function handleDelete(session: NumberedSession) {
    if (!window.confirm(`Delete Session ${session.number}? Its history will be lost.`)) return;
    setError(null);
    try {
      await deleteSession(session.id);
      sessions.refetch();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-xl text-sm text-ink-muted">
          A session is one saved playthrough. Continue an existing story, or start a fresh playthrough without changing another save.
        </p>
        {canStart ? (
          <ButtonLink to={`/campaigns/${campaignId}/setup`}>
            <PlusIcon className="h-4 w-4" /> New session
          </ButtonLink>
        ) : null}
      </div>

      {ended ? (
        <p className="rounded-control border border-line bg-surface-sunken px-3 py-2 text-sm text-ink-muted">
          This campaign has ended, so new sessions cannot be started. Reopen it from Campaign settings to keep playing.
        </p>
      ) : null}
      <ErrorNotice error={error} />

      <section aria-labelledby="sessions-title" className="space-y-2">
        <h3 id="sessions-title" className="text-sm font-semibold">Saved playthroughs</h3>
        {sessions.error && numbered === undefined ? (
          <ErrorNotice error={sessions.error} onRetry={sessions.refetch} />
        ) : numbered === undefined ? (
          <LoadingText />
        ) : numbered.length === 0 ? (
          <EmptyState title="No saved playthroughs yet">
            {ended
              ? "This campaign ended before a playthrough was started."
              : canStart
                ? "Choose a hero and start your first saved playthrough."
                : "Resolve the campaign readiness step before starting a playthrough."}
          </EmptyState>
        ) : (
          <ul className="space-y-2">
            {numbered.map((session) => {
              const who = session.active_character_id == null
                ? "No hero"
                : session.character_name || "Character removed";
              const canRead = session.readiness?.play_allowed === true ||
                (session.has_history && session.readiness?.history_allowed === true);
              return (
                <li key={session.id} className={cn(cardClasses, "flex flex-wrap items-center justify-between gap-3 px-4 py-3")}>
                  <div className="min-w-0">
                    <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                      Playthrough {session.number}
                      {session.ended_at ? <Pill>Finished</Pill> : null}
                    </p>
                    {session.has_history && session.last_played_at ? (
                      <p className="text-xs text-ink-subtle">Last played {relativeTime(session.last_played_at)}</p>
                    ) : <p className="text-xs text-ink-subtle">No story history yet</p>}
                    {session.readiness?.play_allowed !== true ? (
                      <ReadinessNotice readiness={session.readiness} onRetry={sessions.refetch} className="mt-2 text-xs" />
                    ) : null}
                    <p className="break-words text-xs text-ink-muted">
                      {who} · started <time dateTime={session.started_at} title={dateTime(session.started_at)}>{relativeTime(session.started_at)}</time>
                    </p>
                    {isDev ? <p className="font-mono text-[11px] text-ink-subtle">session #{session.id}{session.active_character_id != null ? ` · char #${session.active_character_id}` : ""}{session.current_scene_id ? ` · scene ${session.current_scene_id}` : ""}</p> : null}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {canRead ? (
                      <Button size="sm" onClick={() => play(session)}>
                        <PlayIcon className="h-3 w-3" /> {session.readiness?.play_allowed === true ? "Continue" : "Read story"}
                      </Button>
                    ) : null}
                    <details className="relative">
                      <summary className="inline-flex min-h-9 cursor-pointer list-none items-center rounded-control border border-line px-3 text-sm text-ink-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400">More</summary>
                      <div className="absolute right-0 z-10 mt-1 rounded-control border border-line bg-surface-raised p-2 shadow-lg">
                        <Button variant="danger" size="sm" onClick={() => void handleDelete(session)} aria-label={`Delete Playthrough ${session.number}`}>Delete playthrough</Button>
                      </div>
                    </details>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <p className="text-xs text-ink-subtle">Manage your roster on the <Link to="/characters" className="underline decoration-ember-500 underline-offset-2">Characters</Link> page.</p>
    </div>
  );
}
