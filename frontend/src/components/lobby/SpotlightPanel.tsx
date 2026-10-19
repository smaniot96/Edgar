import { useState } from "react";

import { updateSpotlight } from "../../api/client";
import type { SpotlightMode, SpotlightState } from "../../api/sessionChannel";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";
import { ErrorNotice } from "../ui/ErrorNotice";
import { Pill } from "../Pill";
import { cardClasses, controlClasses } from "../ui/styles";
import { cn } from "../../lib/cn";
import { MODE_LABEL, type RosterRow } from "./tableState";

const MODE_HINT: Record<SpotlightMode, string> = {
  round_robin: "The spotlight passes to the next seated player after each turn.",
  owner_directed: "Only the owner hands the spotlight over.",
  free_for_all: "Any seated player may act when nobody holds the spotlight.",
};

const HEADING = "font-display text-lg text-ink";

/** Read-only turn order, shown to every role: who is acting and who is next. */
export function SpotlightPanel({ spotlight, selfUserId }: { spotlight: SpotlightState | null; selfUserId: number | null }) {
  return (
    <section aria-labelledby="spotlight-title" className={cn(cardClasses, "space-y-3 p-4")}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="spotlight-title" className={HEADING}>Whose turn</h2>
        {spotlight?.engaged ? <Pill tone="info">{MODE_LABEL[spotlight.mode]}</Pill> : null}
      </div>
      {!spotlight ? (
        <p className="text-sm text-ink-muted">Reading the turn order…</p>
      ) : !spotlight.engaged ? (
        <p className="text-sm text-ink-muted">
          Free play. Turn order starts once two or more heroes are seated at this table.
        </p>
      ) : (
        <>
          <p className="text-sm" data-testid="spotlight-holder">
            <span className="font-semibold text-ink-subtle">Now acting: </span>
            {spotlight.holder ? (
              <strong className="text-ink">
                {spotlight.holder.character_name}
                {spotlight.holder.display_name ? ` (${spotlight.holder.display_name})` : ""}
                {spotlight.holder.user_id === selfUserId ? " — you" : ""}
              </strong>
            ) : (
              <span className="text-ink-muted">Nobody. The spotlight is open.</span>
            )}
          </p>
          {spotlight.handover_due ? (
            <p className="rounded-control bg-warning-soft px-3 py-2 text-xs text-warning">
              The holder has been idle past the timeout. The spotlight moves on with the next action at the table.
            </p>
          ) : null}
          {spotlight.next_up.length > 0 ? (
            <div>
              <p className="text-xs font-semibold text-ink-subtle">Next up</p>
              <ol className="mt-1 space-y-1 text-sm" aria-label="Next up">
                {spotlight.next_up.map((member, index) => (
                  <li key={member.character_id} className="flex flex-wrap items-center gap-2">
                    <span className="w-5 text-ink-subtle">{index + 1}.</span>
                    <span>{member.character_name}</span>
                    {member.display_name ? <span className="text-xs text-ink-muted">{member.display_name}</span> : null}
                    {member.present ? null : <Pill>Away</Pill>}
                  </li>
                ))}
              </ol>
            </div>
          ) : (
            <p className="text-xs text-ink-muted">No one else is eligible right now.</p>
          )}
        </>
      )}
    </section>
  );
}

/** Owner-only: mode, idle timeout, hand-over and release. The server arbitrates; stale clicks are refused. */
export function OwnerSpotlightControls({
  sessionId,
  spotlight,
  seated,
  onChanged,
}: {
  sessionId: number;
  spotlight: SpotlightState | null;
  seated: RosterRow[];
  onChanged: () => void;
}) {
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [timeout, setTimeoutValue] = useState<string | null>(null);

  async function run(key: string, body: Parameters<typeof updateSpotlight>[1]) {
    setBusy(key);
    setError(null);
    try {
      await updateSpotlight(sessionId, { ...body, ...(spotlight ? { expected_version: spotlight.version } : {}) });
      onChanged();
    } catch (err) {
      setError(err);
      onChanged();
    } finally {
      setBusy(null);
    }
  }

  if (!spotlight?.engaged) {
    return (
      <section aria-labelledby="spotlight-controls-title" className={cn(cardClasses, "space-y-2 p-4")}>
        <h2 id="spotlight-controls-title" className={HEADING}>Spotlight controls</h2>
        <p className="text-sm text-ink-muted">
          Turn order needs at least two seated heroes. Invite a player with a session invite to start sharing the spotlight.
        </p>
      </section>
    );
  }
  const timeoutText = timeout ?? String(spotlight.timeout_seconds);
  const timeoutNumber = Number(timeoutText);
  const timeoutValid = Number.isInteger(timeoutNumber) && timeoutNumber >= 30 && timeoutNumber <= 3600;
  return (
    <section aria-labelledby="spotlight-controls-title" className={cn(cardClasses, "space-y-4 p-4")}>
      <h2 id="spotlight-controls-title" className={HEADING}>Spotlight controls</h2>
      <ErrorNotice error={error} />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Turn mode" hint={MODE_HINT[spotlight.mode]}>
          {(p) => (
            <select
              {...p}
              className={controlClasses}
              value={spotlight.mode}
              disabled={busy !== null}
              onChange={(e) => void run("mode", { mode: e.target.value as SpotlightMode })}
            >
              {(Object.keys(MODE_LABEL) as SpotlightMode[]).map((mode) => (
                <option key={mode} value={mode}>{MODE_LABEL[mode]}</option>
              ))}
            </select>
          )}
        </Field>
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (timeoutValid) void run("timeout", { timeout_seconds: timeoutNumber }).then(() => setTimeoutValue(null));
          }}
        >
          <Field
            className="flex-1"
            label="Idle timeout (seconds)"
            hint="After this long without an action the spotlight moves on. 30 to 3600."
            error={timeoutValid ? undefined : "Enter a whole number from 30 to 3600."}
          >
            {(p) => (
              <input
                {...p}
                type="number"
                inputMode="numeric"
                min={30}
                max={3600}
                className={controlClasses}
                value={timeoutText}
                onChange={(e) => setTimeoutValue(e.target.value)}
              />
            )}
          </Field>
          <Button type="submit" variant="secondary" disabled={!timeoutValid || busy !== null || timeoutNumber === spotlight.timeout_seconds} loading={busy === "timeout"} loadingText="Saving…" className="mb-[1.35rem]">
            Save
          </Button>
        </form>
      </div>
      <div>
        <p className="text-xs font-semibold text-ink-subtle">Hand over the spotlight</p>
        {seated.length === 0 ? (
          <p className="mt-1 text-sm text-ink-muted">No heroes are seated.</p>
        ) : (
          <ul className="mt-2 flex flex-wrap gap-2">
            {seated.map((row) => {
              const current = spotlight.holder?.character_id === row.characterId;
              return (
                <li key={row.userId}>
                  <Button
                    size="sm"
                    variant={current ? "primary" : "secondary"}
                    disabled={current || busy !== null}
                    aria-pressed={current}
                    onClick={() => void run(`holder:${row.characterId}`, { holder_character_id: row.characterId })}
                  >
                    {current ? `${row.characterName} has it` : `Give to ${row.characterName}`}
                  </Button>
                </li>
              );
            })}
            <li>
              <Button
                size="sm"
                variant="ghost"
                disabled={spotlight.holder === null || busy !== null}
                onClick={() => void run("release", { holder_character_id: null })}
              >
                Release
              </Button>
            </li>
          </ul>
        )}
      </div>
    </section>
  );
}
