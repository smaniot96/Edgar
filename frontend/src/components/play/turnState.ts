/**
 * Pure state machine for the play chat: the committed transcript plus the in-flight turn.
 * Kept free of React/fetch so it can be unit-tested; `useTurn` drives it.
 */
import type {
  AdjudicationResult,
  ChatMessageRead,
  CombatOutcome,
  DebugPayload,
  RollStatus,
  TurnResult,
  SourceCitation,
} from "../../api/types";
import { adjudicationFromRoll } from "./rollText";

interface BaseLine {
  id: string;
  /** Turn that produced this line (absent for history/intro lines). */
  turnId?: string;
  /** Player-safe narration event used to open this line from a journal entry. */
  eventId?: number;
  /** Transport ended before the server's saved outcome could be confirmed. */
  uncertain?: boolean;
}

export interface UserLine extends BaseLine {
  role: "user";
  content: string;
  /** The turn failed, so this action was never recorded by the DM. */
  failed?: boolean;
}

export interface DmLine extends BaseLine {
  citations?: SourceCitation[];
  ruling_citation_ids?: string[];
  role: "dm";
  content: string;
  adjudication?: AdjudicationResult | null;
  debug?: DebugPayload | null;
  combatOutcome?: CombatOutcome;
  /** Reloaded turns only: whether the original roll was stored (EDG-030). */
  rollStatus?: RollStatus;
  /** True for DM turns produced in this browser session (eligible for a dev panel). */
  isTurn?: boolean;
  /** Narration was cut off (stream ended early / stopped) and was not saved. */
  interrupted?: boolean;
}

export interface ErrorLine extends BaseLine {
  role: "error";
  content: string;
  code?: string;
  /** Player input to re-send when the user hits Retry. */
  retryInput?: string;
  /** Stable client ID reused if this action needs a post-disconnect retry. */
  retryRequestId?: string;
  draftRevision?: number;
  /** An authoritative receipt check is needed before this line can be retried. */
  uncertain?: boolean;
}

export interface NoticeLine extends BaseLine {
  role: "notice";
  content: string;
}

export type Line = UserLine | DmLine | ErrorLine | NoticeLine;

export interface PendingTurn {
  turnId: string;
  requestId: string;
  draftRevision?: number;
  input: string;
  stage: string | null;
  /** Narration streamed so far. */
  text: string;
  adjudication: AdjudicationResult | null;
  debug: DebugPayload | null;
}

export interface TurnState {
  /** Boot transcript has reached this reducer, so reading anchors may be restored. */
  historyReady: boolean;
  lines: Line[];
  pending: PendingTurn | null;
  /** Latest message for the screen-reader status region (completed narration / errors). */
  announcement: string;
}

export type FailReason = "error" | "stopped" | "ended";

export type TurnAction =
  | { type: "reset"; lines: Line[] }
  | { type: "append"; line: Line }
  | { type: "recover"; turnId: string; requestId: string; input: string; draftRevision?: number }
  | { type: "start"; turnId: string; requestId: string; draftRevision?: number; input: string }
  | { type: "status"; turnId: string; stage: string }
  | { type: "token"; turnId: string; text: string }
  | { type: "adjudication"; turnId: string; result: AdjudicationResult }
  | { type: "debug"; turnId: string; payload: DebugPayload }
  | { type: "done"; turnId: string; result: TurnResult }
  | {
      type: "fail";
      turnId: string;
      reason: FailReason;
      message: string;
      code?: string;
      uncertain?: boolean;
    }
  | { type: "markRetryable"; turnId: string; message: string }
  | { type: "receiptCommitted"; turnId: string; result: TurnResult }
  | { type: "receiptPending"; turnId: string; message: string }
  /** Remove every line produced by a (failed) turn, e.g. before retrying it. */
  | { type: "clearTurn"; turnId: string };

export const initialTurnState: TurnState = { lines: [], pending: null, announcement: "", historyReady: false };

export function linesFromHistory(msgs: ChatMessageRead[]): Line[] {
  return msgs.map((m, i) => {
    if (m.role === "user") return { id: `h${i}`, role: "user", content: m.content };
    // Reloaded DM turns get their stored ruling back (same card as live); no re-roll happens here.
    // Legacy turns have no stored record: the card is withheld and the line says so.
    const rollStatus: RollStatus = m.roll_status ?? "not_captured";
    return {
      id: `h${i}`,
      role: "dm",
      content: m.content,
      eventId: (m as ChatMessageRead & { event_id?: number }).event_id,
      citations: m.citations,
      ruling_citation_ids: m.ruling_citation_ids,
      adjudication: m.roll ? adjudicationFromRoll(m.roll) : null,
      rollStatus,
      isTurn: true,
    };
  });
}

const CAMPAIGN_ENDED_TEXT = "This campaign has ended. The adventure is complete.";

export function turnReducer(state: TurnState, action: TurnAction): TurnState {
  switch (action.type) {
    case "reset":
      return { ...initialTurnState, lines: action.lines, historyReady: true };
    case "append":
      return { ...state, lines: [...state.lines, action.line] };
    case "recover":
      return {
        ...state,
        lines: [...state.lines,
          { id: `${action.turnId}:user`, turnId: action.turnId, role: "user", content: action.input, failed: true, uncertain: true },
          { id: `${action.turnId}:error`, turnId: action.turnId, role: "error", content: "Checking whether this action was saved…", retryInput: action.input, retryRequestId: action.requestId, draftRevision: action.draftRevision, uncertain: true },
        ],
      };
    case "clearTurn":
      return { ...state, lines: state.lines.filter((l) => l.turnId !== action.turnId) };
    case "start":
      return {
        ...state,
        lines: [
          ...state.lines,
          { id: `${action.turnId}:user`, turnId: action.turnId, role: "user", content: action.input },
        ],
        pending: {
          turnId: action.turnId,
          requestId: action.requestId,
          draftRevision: action.draftRevision,
          input: action.input,
          stage: "parsing",
          text: "",
          adjudication: null,
          debug: null,
        },
      };
  }

  if (action.type === "markRetryable") {
    return {
      ...state,
      lines: state.lines.map((line) => line.turnId === action.turnId
        ? { ...line, ...(line.role === "error" ? { content: action.message } : {}), uncertain: false }
        : line),
    };
  }

  if (action.type === "receiptCommitted") {
    const r = action.result;
    const eventId = (r as TurnResult & { event_id?: number }).event_id;
    const narration = typeof r.narration === "string" && r.narration.trim() ? r.narration : "[no narration]";
    const existingUser = state.lines.find((line) => line.turnId === action.turnId && line.role === "user");
    const lines = state.lines.filter((line) => line.turnId !== action.turnId);
    const alreadyInHistory = eventId != null && lines.some((line) => line.role === "dm" && line.eventId === eventId);
    if (alreadyInHistory) return { ...state, lines, pending: null, announcement: "Saved action confirmed." };
    if (existingUser?.role === "user") lines.push({ ...existingUser, failed: false, uncertain: false });
    lines.push({
      id: `${action.turnId}:dm`,
      turnId: action.turnId,
      role: "dm",
      content: narration,
      eventId,
      citations: r.citations,
      ruling_citation_ids: r.ruling_citation_ids,
      adjudication: r.roll ? adjudicationFromRoll(r.roll) : null,
      rollStatus: r.roll_status ?? "not_captured",
      combatOutcome: r.combat_state?.ended ? r.combat_state.outcome : undefined,
      isTurn: true,
    });
    return { ...state, lines, pending: null, announcement: narration };
  }

  if (action.type === "receiptPending") {
    return {
      ...state,
      lines: state.lines.map((line) => line.role === "error" && line.turnId === action.turnId
        ? { ...line, content: action.message }
        : line),
    };
  }

  // Everything below applies to the in-flight turn only; ignore stale events.
  const p = state.pending;
  if (!p || p.turnId !== action.turnId) return state;

  switch (action.type) {
    case "status":
      return { ...state, pending: { ...p, stage: action.stage || null } };
    case "token":
      return { ...state, pending: { ...p, text: p.text + action.text } };
    case "adjudication":
      return { ...state, pending: { ...p, adjudication: action.result } };
    case "debug":
      return { ...state, pending: { ...p, debug: action.payload } };
    case "done": {
      const r = action.result;
      const content =
        typeof r.narration === "string" && r.narration.trim() ? r.narration : "[no narration]";
      const line: DmLine = {
        id: `${p.turnId}:dm`,
        turnId: p.turnId,
        role: "dm",
        content,
        eventId: (r as TurnResult & { event_id?: number }).event_id,
        citations: r.citations,
        ruling_citation_ids: r.ruling_citation_ids,
        adjudication: p.adjudication,
        debug: p.debug,
        combatOutcome: r.combat_state?.ended ? r.combat_state.outcome : undefined,
        isTurn: true,
      };
      return { ...state, lines: [...state.lines, line], pending: null, announcement: content };
    }
    case "fail": {
      if (action.reason === "ended") {
        return {
          ...state,
          lines: [
            ...state.lines.filter((l) => l.turnId !== p.turnId),
            { id: `${p.turnId}:notice`, role: "notice", content: CAMPAIGN_ENDED_TEXT },
          ],
          pending: null,
          announcement: CAMPAIGN_ENDED_TEXT,
        };
      }
      const lines: Line[] = state.lines.map((l) =>
        l.turnId === p.turnId && l.role === "user" ? { ...l, failed: true, uncertain: action.uncertain } : l,
      );
      if (p.text.trim()) {
        lines.push({
          id: `${p.turnId}:dm`,
          turnId: p.turnId,
          role: "dm",
          content: p.text,
          adjudication: p.adjudication,
          debug: p.debug,
          isTurn: true,
          interrupted: true,
          uncertain: action.uncertain,
        });
      }
      lines.push({
        id: `${p.turnId}:error`,
        turnId: p.turnId,
        role: "error",
        content: action.message,
        code: action.code,
        retryInput: p.input,
        retryRequestId: p.requestId,
        draftRevision: p.draftRevision,
        uncertain: action.uncertain,
      });
      return { ...state, lines, pending: null, announcement: action.message };
    }
  }
  return state;
}
