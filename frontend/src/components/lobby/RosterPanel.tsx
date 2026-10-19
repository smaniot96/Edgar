import { useState } from "react";

import { changeMemberRole, removeMember } from "../../api/client";
import type { MemberRole } from "../../api/types";
import { cn } from "../../lib/cn";
import { Pill } from "../Pill";
import { Button } from "../ui/Button";
import { ErrorNotice } from "../ui/ErrorNotice";
import { cardClasses, controlClasses } from "../ui/styles";
import type { RosterRow } from "./tableState";

const ROLE_LABEL: Record<MemberRole, string> = { owner: "Owner", player: "Player", spectator: "Spectator" };

function presenceLabel(row: RosterRow): string {
  if (!row.present) return "Away";
  return row.idle ? "Idle" : "Here";
}

/**
 * Who is at the table: presence, role, seated hero, turn position. The owner also gets role and
 * removal controls here; the server enforces them, and a removed member is cut off live.
 */
export function RosterPanel({
  rows,
  campaignId,
  canManage,
  onChanged,
}: {
  rows: RosterRow[];
  campaignId: number;
  canManage: boolean;
  onChanged: () => void;
}) {
  const [error, setError] = useState<unknown>(null);
  const [busyUser, setBusyUser] = useState<number | null>(null);

  async function act(userId: number, action: () => Promise<unknown>) {
    setBusyUser(userId);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err);
    } finally {
      setBusyUser(null);
      onChanged();
    }
  }

  return (
    <section aria-labelledby="roster-title" className={cn(cardClasses, "space-y-3 p-4")}>
      <h2 id="roster-title" className="font-display text-lg text-ink">At the table</h2>
      <ErrorNotice error={error} />
      {rows.length === 0 ? (
        <p className="text-sm text-ink-muted">Nobody else has joined yet.</p>
      ) : (
        <ul className="divide-y divide-line">
          {rows.map((row) => (
            <li key={row.userId} data-testid={`member-${row.userId}`} className="flex flex-wrap items-center justify-between gap-3 py-3 first:pt-0 last:pb-0">
              <div className="min-w-0">
                <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                  <span
                    aria-hidden="true"
                    className={cn("inline-block h-2.5 w-2.5 rounded-full", row.present ? (row.idle ? "bg-warning" : "bg-success") : "border border-ink-subtle")}
                  />
                  <span className="[overflow-wrap:anywhere]">{row.name}</span>
                  {row.isYou ? <Pill tone="ember">You</Pill> : null}
                  <Pill tone={row.role === "spectator" ? "info" : "neutral"}>{ROLE_LABEL[row.role]}</Pill>
                  {row.isHolder ? <Pill tone="success">Acting now</Pill> : null}
                  {!row.isHolder && row.nextUp !== null ? <Pill>Next up #{row.nextUp}</Pill> : null}
                </p>
                <p className="text-xs text-ink-muted">
                  <span>{presenceLabel(row)}</span>
                  {" · "}
                  {row.characterName ? <span>Playing {row.characterName}</span> : <span>{row.role === "spectator" ? "Watching" : "No hero seated"}</span>}
                </p>
              </div>
              {canManage && row.role !== "owner" ? (
                <div className="flex flex-wrap items-center gap-2">
                  <label className="sr-only" htmlFor={`role-${row.userId}`}>Role of {row.name}</label>
                  <select
                    id={`role-${row.userId}`}
                    className={cn(controlClasses, "h-9 w-auto py-0")}
                    value={row.role}
                    disabled={busyUser !== null}
                    onChange={(e) => {
                      const role = e.target.value as Exclude<MemberRole, "owner">;
                      if (role === "spectator" && row.characterName && !window.confirm(`Make ${row.name} a spectator? Their seat (${row.characterName}) is freed.`)) return;
                      void act(row.userId, () => changeMemberRole(campaignId, row.userId, role));
                    }}
                  >
                    <option value="player">Player</option>
                    <option value="spectator">Spectator</option>
                  </select>
                  <Button
                    size="sm"
                    variant="danger"
                    disabled={busyUser !== null}
                    aria-label={`Remove ${row.name} from the campaign`}
                    onClick={() => {
                      if (!window.confirm(`Remove ${row.name} from this campaign? They lose access at once. Their characters are kept.`)) return;
                      void act(row.userId, () => removeMember(campaignId, row.userId));
                    }}
                  >
                    Remove
                  </Button>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
