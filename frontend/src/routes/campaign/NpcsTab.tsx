import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { createNpc, deleteNpc, listSessions, updateNpc } from "../../api/client";
import type { NPCRead, SessionRead } from "../../api/types";
import { Pill } from "../../components/Pill";
import { Button } from "../../components/ui/Button";
import { EmptyState, LoadingText } from "../../components/ui/EmptyState";
import { ErrorNotice } from "../../components/ui/ErrorNotice";
import { TextField } from "../../components/ui/Field";
import { cardClasses, controlClasses } from "../../components/ui/styles";
import { useMode } from "../../dev/ModeContext";
import { cn } from "../../lib/cn";
import { dateTime } from "../../lib/format";
import { useNpcs } from "../../lib/useNpcs";
import { useQuery } from "../../lib/useQuery";

interface NumberedSession extends SessionRead {
  number: number;
}

function selectedSession(value: string | null): number | null {
  if (value === null || !/^[1-9]\d*$/.test(value)) return null;
  const id = Number(value);
  return Number.isSafeInteger(id) ? id : null;
}

function playLink(sessionId: number, journalEntryId?: string | null): string {
  const query = new URLSearchParams({ tool: "journal" });
  if (journalEntryId) query.set("entry", journalEntryId);
  return `/play/${sessionId}?${query.toString()}`;
}

export default function NpcsTab({ campaignId }: { campaignId: number }) {
  const { isDev } = useMode();
  const [params, setParams] = useSearchParams();
  const selectedValue = params.get("session_id");
  const selectedId = selectedSession(selectedValue);
  const sessions = useQuery(`sessions:${campaignId}`, (signal) => listSessions(campaignId, signal));
  const manualNpcs = useNpcs(campaignId, null);

  const numbered = useMemo<NumberedSession[] | undefined>(() => {
    if (!sessions.data) return undefined;
    return [...sessions.data]
      .sort((a, b) => a.started_at.localeCompare(b.started_at))
      .map((session, index) => ({ ...session, number: index + 1 }))
      .reverse();
  }, [sessions.data]);
  const matchingSession = numbered?.find((session) => session.id === selectedId);
  const invalidSelection = selectedValue !== null && (selectedId === null || (numbered !== undefined && !matchingSession));
  const waitingToValidate = selectedId !== null && numbered === undefined && !sessions.error;
  const sessionNpcs = useNpcs(
    campaignId,
    selectedId,
    selectedId !== null && matchingSession !== undefined,
  );
  const learnedNpcs = (sessionNpcs.data?.npcs ?? []).filter(
    (npc) => npc.session_id === selectedId,
  );

  const [name, setName] = useState("");
  const [disposition, setDisposition] = useState("");
  const [editing, setEditing] = useState<NPCRead | null>(null);
  const [editName, setEditName] = useState("");
  const [editDisposition, setEditDisposition] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  function changeSession(value: string) {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value) next.set("session_id", value);
      else next.delete("session_id");
      next.set("tab", "npcs");
      return next;
    });
  }

  async function add(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await createNpc(campaignId, { name: name.trim(), disposition: disposition.trim() });
      setName("");
      setDisposition("");
      manualNpcs.refetch();
      if (selectedId !== null) sessionNpcs.refetch();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  function startEdit(npc: NPCRead) {
    setEditing(npc);
    setEditName(npc.name);
    setEditDisposition(npc.disposition);
    setError(null);
  }

  async function saveEdit(e: FormEvent) {
    e.preventDefault();
    if (!editing || editing.session_id !== null) return;
    setBusy(true);
    setError(null);
    try {
      await updateNpc(editing.id, { name: editName.trim(), disposition: editDisposition.trim() });
      setEditing(null);
      manualNpcs.refetch();
      if (selectedId !== null) sessionNpcs.refetch();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function remove(npc: NPCRead) {
    if (npc.session_id !== null || !window.confirm(`Delete ${npc.name}?`)) return;
    setError(null);
    try {
      await deleteNpc(npc.id);
      manualNpcs.refetch();
      if (selectedId !== null) sessionNpcs.refetch();
    } catch (err) {
      setError(err);
    }
  }

  const visibleManual = manualNpcs.data?.npcs.filter((npc) => npc.session_id === null) ?? [];

  return (
    <div className="space-y-4">
      <div className={cn(cardClasses, "space-y-3 p-4")}>
        <label htmlFor="npc-session" className="block text-sm font-medium text-ink">
          Saved playthrough
        </label>
        <select
          id="npc-session"
          className={controlClasses}
          value={selectedValue ?? ""}
          onChange={(event) => changeSession(event.target.value)}
        >
          <option value="">Campaign notes only</option>
          {(numbered ?? []).map((session) => (
            <option key={session.id} value={session.id}>
              Session {session.number} · {dateTime(session.started_at)}
            </option>
          ))}
        </select>
        <p className="text-sm text-ink-muted">
          Choose a save to see the people learned in that story. Campaign notes are shared notes you add yourself.
        </p>
      </div>

      <form onSubmit={(event) => void add(event)} className={cn(cardClasses, "space-y-3 p-4")}>
        <h3 className="text-sm font-semibold">Add campaign note</h3>
        <p className="text-sm text-ink-muted">
          These notes belong to the campaign and stay separate from people learned during a saved playthrough.
        </p>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <TextField
            fieldClassName="flex-1"
            label="Name"
            required
            maxLength={255}
            placeholder="Sildar Hallwinter"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
          <TextField
            fieldClassName="flex-1"
            label="Disposition or note"
            required
            maxLength={255}
            placeholder="friendly, hostile, wary…"
            value={disposition}
            onChange={(event) => setDisposition(event.target.value)}
          />
          <Button type="submit" loading={busy} loadingText="Adding…">
            Add note
          </Button>
        </div>
      </form>

      <ErrorNotice error={error} />
      {sessions.error ? <ErrorNotice error={sessions.error} onRetry={sessions.refetch} /> : null}
      {manualNpcs.error && manualNpcs.data === undefined ? (
        <ErrorNotice error={manualNpcs.error} onRetry={manualNpcs.refetch} />
      ) : null}
      {manualNpcs.error && manualNpcs.data !== undefined ? (
        <ErrorNotice error={manualNpcs.error} onRetry={manualNpcs.refetch} />
      ) : null}
      {sessionNpcs.error ? <ErrorNotice error={sessionNpcs.error} onRetry={sessionNpcs.refetch} /> : null}
      {invalidSelection ? (
        <p role="alert" className="rounded-control border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
          That saved playthrough is not part of this campaign. Choose one of this campaign’s sessions.
        </p>
      ) : null}
      {waitingToValidate || manualNpcs.data === undefined ? <LoadingText>Loading campaign notes…</LoadingText> : null}

      {manualNpcs.data !== undefined ? (
        <section aria-labelledby="campaign-notes-title" className="space-y-2">
          <h3 id="campaign-notes-title" className="text-sm font-semibold">Campaign notes · manual</h3>
          {visibleManual.length === 0 ? (
            <EmptyState title="No campaign notes yet">Add a note above if you want to keep campaign-wide reminders.</EmptyState>
          ) : (
            <ul className="space-y-2">
              {visibleManual.map((npc) => (
                <li key={npc.id} className={cn(cardClasses, "px-4 py-3")}>
                  {editing?.id === npc.id ? (
                    <form onSubmit={(event) => void saveEdit(event)} className="space-y-3">
                      <TextField label="Name" required maxLength={255} value={editName} onChange={(event) => setEditName(event.target.value)} />
                      <TextField label="Disposition or note" required maxLength={255} value={editDisposition} onChange={(event) => setEditDisposition(event.target.value)} />
                      <div className="flex gap-2">
                        <Button type="submit" size="sm" loading={busy} loadingText="Saving…">Save note</Button>
                        <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
                      </div>
                    </form>
                  ) : (
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="break-words text-sm font-medium">
                          {npc.name}{isDev ? <span className="ml-2 font-mono text-xs text-ink-subtle">#{npc.id}</span> : null}
                        </p>
                        <Pill className="mt-1">{npc.disposition}</Pill>
                      </div>
                      <div className="flex shrink-0 gap-2">
                        <Button variant="secondary" size="sm" onClick={() => startEdit(npc)}>Edit</Button>
                        <Button variant="danger" size="sm" onClick={() => void remove(npc)} aria-label={`Delete ${npc.name}`}>Delete</Button>
                      </div>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}

      {selectedId !== null && matchingSession ? (
        <section aria-labelledby="learned-people-title" className="space-y-2">
          <h3 id="learned-people-title" className="text-sm font-semibold">People in Session {matchingSession.number}</h3>
          {sessionNpcs.data === undefined ? (
            sessionNpcs.error ? (
              <EmptyState title="Couldn't load this save's people list">
                The request failed, so Edgar can't confirm whether anyone is recorded. Retry to check this save again.
              </EmptyState>
            ) : <LoadingText>Loading people from this save…</LoadingText>
          ) : sessionNpcs.error && learnedNpcs.length === 0 ? (
            <EmptyState title="Couldn't confirm this save's people list">
              The request failed, so Edgar can't tell whether anyone is recorded. Retry to check this save again.
            </EmptyState>
          ) : sessionNpcs.data.tracking === "legacy_untracked" ? (
            <EmptyState title="People were not recorded in this older save">
              This playthrough has history from before automatic people tracking. Its past conversations were not used to invent entries.
            </EmptyState>
          ) : learnedNpcs.length === 0 ? (
            <EmptyState title="No known people in this save">
              People Edgar learns through committed story updates will appear here. Return to the story to continue.
            </EmptyState>
          ) : (
            <ul className="space-y-2">
              {learnedNpcs.map((npc) => (
                <li key={npc.id} className={cn(cardClasses, "flex items-start justify-between gap-3 px-4 py-3")}>
                  <div className="min-w-0">
                    <p className="break-words text-sm font-medium">
                      {npc.name}{isDev ? <span className="ml-2 font-mono text-xs text-ink-subtle">#{npc.id}</span> : null}
                    </p>
                    <Pill className="mt-1">{npc.disposition}</Pill>
                    <p className="mt-1 text-xs text-ink-muted">Tracked from this saved story</p>
                  </div>
                  {npc.journal_entry_id ? (
                    <Link
                      to={playLink(npc.session_id!, npc.journal_entry_id)}
                      className="shrink-0 rounded-control px-2 py-1 text-sm text-ember-300 hover:text-ember-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400"
                      aria-label={`Open ${npc.name} in the story journal`}
                    >
                      Open in journal
                    </Link>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}

      {numbered?.length === 0 ? (
        <p className="text-sm text-ink-muted">Start a session to create a save where encountered people can be tracked.</p>
      ) : null}
      {selectedId !== null && !matchingSession && !waitingToValidate && !sessions.error && numbered ? (
        <p className="text-sm text-ink-muted">The selected playthrough is unavailable. Campaign notes remain visible above.</p>
      ) : null}
    </div>
  );
}
