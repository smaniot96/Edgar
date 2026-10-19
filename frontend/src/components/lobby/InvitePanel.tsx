import { useState } from "react";

import { createInvite, listInvites, revokeInvite } from "../../api/client";
import type { InviteCreated, InviteRead, InviteRole } from "../../api/types";
import { cn } from "../../lib/cn";
import { dateTime } from "../../lib/format";
import { inviteLink } from "../../lib/invite";
import { useQuery } from "../../lib/useQuery";
import { Pill } from "../Pill";
import { Button } from "../ui/Button";
import { ErrorNotice } from "../ui/ErrorNotice";
import { Field } from "../ui/Field";
import { cardClasses, controlClasses } from "../ui/styles";

const STATUS_TONE = { active: "success", accepted: "neutral", revoked: "danger", expired: "warning" } as const;

/**
 * Owner-only invitations. The new link is shown once, with a copy button, and is gone after it is
 * dismissed or the page reloads: the server keeps only a digest and cannot show it again.
 */
export function InvitePanel({ campaignId, sessionId }: { campaignId: number; sessionId: number }) {
  const invites = useQuery(`invites:${campaignId}`, (s) => listInvites(campaignId, s));
  const [role, setRole] = useState<InviteRole>("player");
  const [hours, setHours] = useState(72);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<InviteCreated | null>(null);
  const [copied, setCopied] = useState<"yes" | "no" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [revoking, setRevoking] = useState<number | null>(null);

  async function create() {
    setCreating(true);
    setError(null);
    setCopied(null);
    try {
      // A player invite is bound to this session so accepting it seats their hero here.
      const made = await createInvite(campaignId, {
        role,
        expires_in_hours: hours,
        ...(role === "player" ? { session_id: sessionId } : {}),
      });
      setCreated(made);
      invites.refetch();
    } catch (err) {
      setError(err);
    } finally {
      setCreating(false);
    }
  }

  async function copy(link: string) {
    try {
      await navigator.clipboard.writeText(link);
      setCopied("yes");
    } catch {
      // No clipboard permission: the link stays selectable in the field below.
      setCopied("no");
    }
  }

  async function revoke(invite: InviteRead) {
    setRevoking(invite.id);
    setError(null);
    try {
      await revokeInvite(campaignId, invite.id);
      invites.refetch();
    } catch (err) {
      setError(err);
    } finally {
      setRevoking(null);
    }
  }

  const link = created ? inviteLink(created.token) : null;
  const rows = [...(invites.data ?? [])].reverse();
  return (
    <section aria-labelledby="invites-title" className={cn(cardClasses, "space-y-4 p-4")}>
      <h2 id="invites-title" className="font-display text-lg text-ink">Invite people</h2>
      <p className="text-sm text-ink-muted">
        A link lets one person join. Send it privately: anyone who has it can use it until it expires or you revoke it.
      </p>
      <ErrorNotice error={error} />
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Joins as" hint={role === "player" ? "Takes a seat at this table with a hero." : "Watches only. No seat, no input."}>
          {(p) => (
            <select {...p} className={cn(controlClasses, "w-auto")} value={role} onChange={(e) => setRole(e.target.value as InviteRole)}>
              <option value="player">Player</option>
              <option value="spectator">Spectator</option>
            </select>
          )}
        </Field>
        <Field label="Link expires">
          {(p) => (
            <select {...p} className={cn(controlClasses, "w-auto")} value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              <option value={24}>In 1 day</option>
              <option value={72}>In 3 days</option>
              <option value={168}>In 1 week</option>
              <option value={720}>In 30 days</option>
            </select>
          )}
        </Field>
        <Button onClick={() => void create()} loading={creating} loadingText="Creating…" className="mb-[1.35rem]">
          Create invite link
        </Button>
      </div>

      {link ? (
        <div role="group" aria-label="New invite link" className="space-y-2 rounded-control border border-ember-700 bg-surface-sunken p-3">
          <p className="text-sm font-medium text-ink">Copy this link now. It is shown only once.</p>
          <div className="flex flex-wrap gap-2">
            <label className="sr-only" htmlFor="invite-link">Invite link</label>
            <input
              id="invite-link"
              readOnly
              value={link}
              className={cn(controlClasses, "min-w-0 flex-1 font-mono text-xs")}
              onFocus={(e) => e.currentTarget.select()}
            />
            <Button variant="secondary" onClick={() => void copy(link)}>Copy link</Button>
            <Button variant="ghost" onClick={() => { setCreated(null); setCopied(null); }}>Done</Button>
          </div>
          <p role="status" className="text-xs text-ink-muted">
            {copied === "yes" ? "Link copied." : copied === "no" ? "Could not copy automatically. Select the link above and copy it." : `Joins as ${created?.role}. Expires ${created ? dateTime(created.expires_at) : ""}.`}
          </p>
        </div>
      ) : null}

      <div>
        <h3 className="text-sm font-semibold">Invites so far</h3>
        {invites.error && invites.data === undefined ? (
          <ErrorNotice error={invites.error} onRetry={invites.refetch} className="mt-2" />
        ) : rows.length === 0 ? (
          <p className="mt-1 text-sm text-ink-muted">No invites yet.</p>
        ) : (
          <ul className="mt-2 divide-y divide-line">
            {rows.map((invite) => (
              <li key={invite.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                <span className="flex flex-wrap items-center gap-2">
                  <Pill tone={STATUS_TONE[invite.status]}>{invite.status}</Pill>
                  <span>{invite.role === "player" ? "Player" : "Spectator"}</span>
                  <span className="text-xs text-ink-muted">expires {dateTime(invite.expires_at)}</span>
                </span>
                {invite.status === "active" ? (
                  <Button size="sm" variant="danger" loading={revoking === invite.id} loadingText="Revoking…" onClick={() => void revoke(invite)} aria-label={`Revoke invite ${invite.id}`}>
                    Revoke
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
