/**
 * `/join#token=…`: accept a campaign invite (EDG-046, EDG-050).
 *
 * The token travels in the URL fragment, so it is never sent to a server or written to a log. It is
 * read once, held in memory only, and removed from the address bar. A player invite for a session
 * needs a hero: choose one you own that is already in the campaign. The server answers every bad
 * invite the same way on purpose, so the copy lists the possible reasons instead of guessing one.
 */

import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useLocation } from "react-router-dom";

import { acceptInvite, listCharacters } from "../api/client";
import type { InviteAccepted } from "../api/types";
import { Button, ButtonLink } from "../components/ui/Button";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { ErrorNotice } from "../components/ui/ErrorNotice";
import { Field } from "../components/ui/Field";
import { Page, PageHeader } from "../components/ui/Page";
import { cardClasses, controlClasses } from "../components/ui/styles";
import { cn } from "../lib/cn";
import { joinFailureCopy, tokenFromHash } from "../lib/invite";
import { useQuery } from "../lib/useQuery";

export default function Join() {
  const location = useLocation();
  // Read once: the token lives in memory from here on.
  const [token] = useState(() => tokenFromHash(location.hash));
  const [typed, setTyped] = useState("");
  const [manual, setManual] = useState<string | null>(null);
  const active = token ?? manual;

  useEffect(() => {
    if (token) window.history.replaceState(window.history.state, "", `${location.pathname}${location.search}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Page width="sm">
      <PageHeader title="Join a table" description="Accept an invitation to play or watch a campaign." />
      {active ? (
        <AcceptForm token={active} />
      ) : (
        <form
          className={cn(cardClasses, "space-y-3 p-4")}
          onSubmit={(e) => {
            e.preventDefault();
            const value = tokenFromHash(typed) ?? typed.trim();
            if (value) setManual(value);
          }}
        >
          <p className="text-sm text-ink-muted">Open the link you were sent, or paste the invite code or link here.</p>
          <Field label="Invite link or code">
            {(p) => <input {...p} className={controlClasses} value={typed} autoComplete="off" onChange={(e) => setTyped(e.target.value.includes("#") ? e.target.value.slice(e.target.value.indexOf("#")) : e.target.value)} />}
          </Field>
          <Button type="submit" disabled={!typed.trim()}>Continue</Button>
        </form>
      )}
    </Page>
  );
}

function AcceptForm({ token }: { token: string }) {
  const characters = useQuery("characters:join", (s) => listCharacters(s));
  const [characterId, setCharacterId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [joined, setJoined] = useState<InviteAccepted | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setJoined(await acceptInvite(token, characterId ? Number(characterId) : undefined));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  if (joined) {
    const watching = joined.role === "spectator";
    return (
      <EmptyState
        title={watching ? "You are in as a spectator" : joined.character_id ? "You have a seat at the table" : "You joined the campaign"}
        action={
          <ButtonLink to={joined.session_id ? `/play/${joined.session_id}/table` : `/campaigns/${joined.campaign_id}`}>
            {joined.session_id ? "Go to the table" : "Open the campaign"}
          </ButtonLink>
        }
      >
        {watching ? "You can watch the story live. You cannot act." : "The owner will see you at the table."}
      </EmptyState>
    );
  }

  const failure = error ? joinFailureCopy(error) : null;
  return (
    <form className={cn(cardClasses, "space-y-4 p-4")} onSubmit={(e) => void submit(e)}>
      <p className="text-sm text-ink-muted">
        If this invite seats you at a session, choose the hero you will play. Spectator invites need no hero.
      </p>
      {characters.error && characters.data === undefined ? (
        <ErrorNotice error={characters.error} onRetry={characters.refetch} />
      ) : characters.data === undefined ? (
        <LoadingText>Loading your heroes…</LoadingText>
      ) : (
        <Field
          label="Your hero"
          hint={characters.data.length === 0 ? "You have no characters yet. Create one first if this invite seats you." : "Only a hero that is already in the campaign can take the seat."}
        >
          {(p) => (
            <select {...p} className={controlClasses} value={characterId} onChange={(e) => setCharacterId(e.target.value)}>
              <option value="">No hero (spectator, or join without a seat)</option>
              {characters.data!.map((c) => (
                <option key={c.id} value={c.id}>{c.name} · {c.character_class} {c.level}</option>
              ))}
            </select>
          )}
        </Field>
      )}
      {failure && failure.text ? (
        <p role="alert" className="rounded-control border border-danger-strong/60 bg-danger-soft/60 px-3 py-2 text-sm text-danger">
          {failure.text}{" "}
          {failure.settings ? <Link to="/settings" className="underline">Open Settings</Link> : null}
        </p>
      ) : (
        <ErrorNotice error={error} />
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" loading={busy} loadingText="Joining…">Accept invite</Button>
        <ButtonLink to="/campaigns" variant="ghost">Not now</ButtonLink>
      </div>
    </form>
  );
}
