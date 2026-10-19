/**
 * Live table state for one session (EDG-050): the roster, the spotlight and presence.
 *
 * REST gives the roster (`/lobby`) and the first spotlight; the EDG-047 channel keeps them live.
 * The channel client owns ordering (it drops any record at or below its last sequence number and
 * resumes with `Last-Event-ID` after a drop), and this hook folds the records through a pure
 * reducer. Everything is released on unmount: the subscription is aborted and no timer survives.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { getSessionLobby, getSpotlight, isNotFound, reportPresence } from "../../api/client";
import { createSessionChannel, type ChannelEnvelope, type ChannelStatus } from "../../api/sessionChannel";
import type { SessionLobby } from "../../api/types";
import {
  buildRoster,
  initialTableState,
  tableReducer,
  turnGate,
  type RosterRow,
  type TableAction,
  type TableState,
  type TurnGate,
} from "./tableState";

export interface TurnFinishedInfo {
  envelope: ChannelEnvelope;
  /** The turn was submitted by this viewer (their own stream already shows it). */
  own: boolean;
  outcome: string;
}

export interface UseSessionLobbyOptions {
  /** A shared turn ended: the play screen re-reads committed state when it was not the viewer's own. */
  onTurnFinished?: (info: TurnFinishedInfo) => void;
  retryMinMs?: number;
  retryMaxMs?: number;
  /** Skip the live subscription (for example, a screen that only needs the roster once). */
  live?: boolean;
}

export interface SessionLobbyState extends TableState {
  lobby: SessionLobby | null;
  /** REST roster load failed (offline or server error). */
  lobbyError: unknown;
  /** The roster request settled, successfully or not. */
  loaded: boolean;
  /** Membership is gone: the lobby is 404 or the channel closed with `access_revoked`. */
  revoked: boolean;
  status: ChannelStatus | "idle";
  roster: RosterRow[];
  gate: TurnGate;
  /** Re-read the roster and the spotlight (after an owner action, or on demand). */
  refresh: () => void;
}

type SelfAction = TableAction & { self?: number | null };

function reduce(state: TableState, action: SelfAction): TableState {
  return tableReducer(state, action, action.self ?? null);
}

export function useSessionLobby(sessionId: number | null, options: UseSessionLobbyOptions = {}): SessionLobbyState {
  const live = options.live !== false;
  const [lobby, setLobby] = useState<SessionLobby | null>(null);
  const [lobbyError, setLobbyError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const [revokedByRest, setRevokedByRest] = useState(false);
  const [revokedByChannel, setRevokedByChannel] = useState(false);
  const [status, setStatus] = useState<ChannelStatus | "idle">("idle");
  const [nonce, setNonce] = useState(0);
  const [table, dispatchTable] = useReducer(reduce, initialTableState);

  const selfRef = useRef<number | null>(null);
  const callbacks = useRef(options);
  useEffect(() => {
    callbacks.current = options;
  });

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  // Roster and first spotlight over REST.
  useEffect(() => {
    if (sessionId === null) return;
    const ctrl = new AbortController();
    (async () => {
      try {
        const [roster, spotlight] = await Promise.all([
          getSessionLobby(sessionId, ctrl.signal),
          getSpotlight(sessionId, ctrl.signal).catch(() => null),
        ]);
        if (ctrl.signal.aborted) return;
        selfRef.current = roster.viewer.user_id;
        setLobby(roster);
        setLobbyError(null);
        setRevokedByRest(false);
        if (spotlight) dispatchTable({ type: "spotlight", spotlight, self: roster.viewer.user_id });
      } catch (err) {
        if (ctrl.signal.aborted) return;
        if (isNotFound(err)) setRevokedByRest(true);
        else setLobbyError(err);
      } finally {
        if (!ctrl.signal.aborted) setLoaded(true);
      }
    })();
    return () => ctrl.abort();
  }, [sessionId, nonce]);

  // The live channel: ordered, de-duplicated, resumed with Last-Event-ID, stopped on unmount. It opens
  // once the roster request settled, so "is this turn mine" is known before the first record arrives.
  useEffect(() => {
    if (sessionId === null || !live || !loaded) return;
    const channel = createSessionChannel(
      { sessionId, retryMinMs: callbacks.current.retryMinMs, retryMaxMs: callbacks.current.retryMaxMs },
      {
        onEvent: (envelope) => {
          dispatchTable({ type: "event", envelope, self: selfRef.current });
          if (envelope.type === "turn.finished") {
            const outcome = typeof envelope.data.outcome === "string" ? envelope.data.outcome : "unknown";
            const own = (envelope.actor?.user_id ?? null) === selfRef.current;
            callbacks.current.onTurnFinished?.({ envelope, own, outcome });
          }
        },
        onSnapshot: (envelope) => dispatchTable({ type: "snapshot", envelope, self: selfRef.current }),
        onStatus: (next, detail) => {
          // Cleanup reports "stopped"; applying it on unmount would only set state on a dead hook.
          if (next === "closed" && detail === "stopped") return;
          setStatus(next);
          if (next === "closed" && detail === "access") setRevokedByChannel(true);
        },
      },
    );
    return () => channel.stop();
  }, [sessionId, live, loaded]);

  // A seat change means the REST roster is stale.
  const staleSeen = useRef(0);
  useEffect(() => {
    if (table.rosterStale === staleSeen.current) return;
    staleSeen.current = table.rosterStale;
    refresh();
  }, [table.rosterStale, refresh]);

  // Someone present who is not on the roster (just joined): re-read it, once per new arrival.
  const knownIds = useMemo(() => new Set(lobby?.members.map((m) => m.user_id) ?? []), [lobby]);
  const unknownKey = useMemo(
    () => Object.keys(table.presence).filter((id) => !knownIds.has(Number(id))).sort().join(","),
    [knownIds, table.presence],
  );
  const refreshedFor = useRef("");
  useEffect(() => {
    if (!loaded || !lobby || !unknownKey || refreshedFor.current === unknownKey) return;
    refreshedFor.current = unknownKey;
    refresh();
  }, [loaded, lobby, unknownKey, refresh]);

  // Tell the table when this tab is in the background (best effort).
  useEffect(() => {
    if (sessionId === null || !live || typeof document === "undefined") return;
    const report = () => {
      void reportPresence(sessionId, document.visibilityState === "hidden" ? "idle" : "active").catch(() => {});
    };
    document.addEventListener("visibilitychange", report);
    return () => document.removeEventListener("visibilitychange", report);
  }, [sessionId, live]);

  const roster = useMemo(() => buildRoster(lobby, table.presence, table.spotlight), [lobby, table.presence, table.spotlight]);
  const gate = useMemo(() => turnGate(lobby?.viewer ?? null, table.spotlight), [lobby, table.spotlight]);

  return {
    ...table,
    lobby,
    lobbyError,
    loaded,
    revoked: revokedByRest || revokedByChannel,
    status,
    roster,
    gate,
    refresh,
  };
}
