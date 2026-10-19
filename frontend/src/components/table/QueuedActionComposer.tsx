import { useState } from "react";

import { errorMessage } from "../../api/client";
import type { SpotlightState } from "../../api/sessionChannel";
import {
  cancelQueuedAction,
  canQueue,
  editQueuedAction,
  getQueue,
  myQueuedAction,
  queueAction,
  queuedStatusText,
  waitingState,
} from "../../api/tablePacing";
import type { QueuedAction, QueueState } from "../../api/tablePacing";
import { Button } from "../ui/Button";
import { Pill } from "../Pill";

/**
 * Waiting state and the queued-action composer for a player whose turn is not up (EDG-051).
 *
 * Shows who the table is waiting on and where the viewer stands, lets the viewer queue ONE intended
 * action, and shows it (with its status) with edit and cancel. When it is the viewer's turn and an
 * action is queued, `onPlay(item)` hands it to the caller, which submits a normal turn with
 * `queuedTurnFields(item)`; the server validates it again and refuses a stale one. Nothing here
 * orders turns. The server also tells the table that an action is queued (never its text).
 *
 * Mount (EDG-050 lobby), for a seated player: `<QueuedActionComposer sessionId={id}
 * viewerUserId={me.id} state={spotlight} now={now} onQueue={(q) => setSpotlight({ ...spotlight, queue: q })}
 * onPlay={(item) => submitTurn(queuedTurnFields(item))} />`, above or in place of the turn composer
 * while it is not the viewer's turn. `now` is a ms clock the lobby ticks once a second.
 */
export function QueuedActionComposer({
  sessionId,
  viewerUserId,
  state,
  now,
  onQueue,
  onPlay,
}: {
  sessionId: number;
  viewerUserId: number;
  state: SpotlightState;
  now: number;
  onQueue?: (queue: QueueState) => void;
  onPlay?: (item: QueuedAction) => void;
}) {
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wait = waitingState(state, viewerUserId, now);
  const mine = myQueuedAction(state, viewerUserId);
  if (wait.kind === "not_engaged") return null;

  const run = async (work: () => Promise<unknown>, after?: () => void) => {
    setBusy(true);
    setError(null);
    try {
      await work();
      after?.();
      onQueue?.(await getQueue(sessionId));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const submit = () =>
    run(
      () =>
        mine && editing
          ? editQueuedAction(sessionId, mine, draft.trim())
          : queueAction(sessionId, { text: draft.trim(), spotlightVersion: state.version }),
      () => {
        setDraft("");
        setEditing(false);
      },
    );

  return (
    <section className="space-y-2" aria-label="Waiting and queued action">
      {wait.message ? (
        <p className="text-sm text-ink" aria-live="polite">
          {wait.message}
          {wait.secondsLeft !== null && wait.kind === "waiting" ? ` They have ${wait.secondsLeft}s left.` : ""}
          {wait.holderAbsent ? " They are away from the table." : ""}
        </p>
      ) : null}

      {mine ? (
        <div className="space-y-2 rounded-control border border-line-strong p-3">
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={mine.status === "queued" ? "info" : "warning"}>{queuedStatusText(mine)}</Pill>
            <span className="text-xs text-ink-muted">Your queued action</span>
          </div>
          {!editing ? <p className="whitespace-pre-wrap text-sm text-ink">{mine.text}</p> : null}
          {mine.status === "stale" ? (
            <p className="text-xs text-ink-muted">
              It will not be played as written. Edit it for the new situation, or cancel it.
            </p>
          ) : null}
          {mine.status === "expired" ? (
            <p className="text-xs text-ink-muted">It expired. Cancel it and queue a new one.</p>
          ) : null}
          <div className="flex flex-wrap gap-2">
            {wait.kind === "your_turn" && mine.status === "queued" ? (
              <Button size="sm" disabled={busy} onClick={() => onPlay?.(mine)}>
                Play my queued action
              </Button>
            ) : null}
            {mine.status !== "expired" && !editing ? (
              <Button
                size="sm"
                variant="secondary"
                disabled={busy}
                onClick={() => {
                  setDraft(mine.text ?? "");
                  setEditing(true);
                }}
              >
                Edit
              </Button>
            ) : null}
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => void run(() => cancelQueuedAction(sessionId, mine.id), () => setEditing(false))}
            >
              Cancel queued action
            </Button>
          </div>
        </div>
      ) : null}

      {(canQueue(state, viewerUserId) || editing) && (
        <div className="space-y-2">
          <label className="block text-sm font-medium text-ink" htmlFor="queued-action-text">
            {editing ? "Edit your queued action" : "Queue what you want to do next"}
          </label>
          <textarea
            id="queued-action-text"
            className="w-full rounded-control border border-line-strong bg-surface p-2 text-sm text-ink"
            rows={2}
            maxLength={2000}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={busy}
          />
          <div className="flex gap-2">
            <Button size="sm" disabled={busy || draft.trim() === ""} onClick={() => void submit()}>
              {editing ? "Save changes" : "Queue action"}
            </Button>
            {editing ? (
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(false)}>
                Keep as is
              </Button>
            ) : null}
          </div>
          <p className="text-xs text-ink-muted">
            The table sees that you have queued an action, not what it says. It is checked again on your
            turn and is not played if the situation has changed.
          </p>
        </div>
      )}
      {error ? (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      ) : null}
    </section>
  );
}
