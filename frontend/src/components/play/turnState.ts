/**
 * Pure state machine for the play chat: the committed transcript plus the in-flight turn.
 * Kept free of React/fetch so it can be unit-tested; `useTurn` drives it.
 */
import type {
  AdjudicationResult,
  ChatMessageRead,
  CombatOutcome,
  DebugPayload,
  TurnResult,
} from "../../api/types";

interface BaseLine {
  id: string;
  /** Turn that produced this line (absent for history/intro lines). */
  turnId?: string;
}

export interface UserLine extends BaseLine {
  role: "user";
  content: string;
  /** The turn failed, so this action was never recorded by the DM. */
  failed?: boolean;
}

export interface DmLine extends BaseLine {
  role: "dm";
  content: string;
  adjudication?: AdjudicationResult | null;
  debug?: DebugPayload | null;
  combatOutcome?: CombatOutcome;
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
}

export interface NoticeLine extends BaseLine {
  role: "notice";
  content: string;
}

export type Line = UserLine | DmLine | ErrorLine | NoticeLine;

export interface PendingTurn {
  turnId: string;
  input: string;
  stage: string | null;
  /** Narration streamed so far. */
  text: string;
  adjudication: AdjudicationResult | null;
  debug: DebugPayload | null;
}

export interface TurnState {
  lines: Line[];
  pending: PendingTurn | null;
  /** Latest message for the screen-reader status region (completed narration / errors). */
  announcement: string;
}

export type FailReason = "error" | "stopped" | "ended";

export type TurnAction =
  | { type: "reset"; lines: Line[] }
  | { type: "append"; line: Line }
  | { type: "start"; turnId: string; input: string }
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
    }
  /** Remove every line produced by a (failed) turn, e.g. before retrying it. */
  | { type: "clearTurn"; turnId: string };

export const initialTurnState: TurnState = { lines: [], pending: null, announcement: "" };

export function linesFromHistory(msgs: ChatMessageRead[]): Line[] {
  return msgs.map((m, i) =>
    m.role === "user"
      ? { id: `h${i}`, role: "user", content: m.content }
      : // Past DM turns have no captured debug; the dev panel says so in dev mode.
        { id: `h${i}`, role: "dm", content: m.content, isTurn: true },
  );
}

const CAMPAIGN_ENDED_TEXT = "This campaign has ended. The adventure is complete.";

export function turnReducer(state: TurnState, action: TurnAction): TurnState {
  switch (action.type) {
    case "reset":
      return { ...initialTurnState, lines: action.lines };
    case "append":
      return { ...state, lines: [...state.lines, action.line] };
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
          input: action.input,
          stage: "parsing",
          text: "",
          adjudication: null,
          debug: null,
        },
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
      const fallback =
        typeof r.narration === "string" && r.narration.trim() ? r.narration : "[no narration]";
      const content = p.text.trim() ? p.text : fallback;
      const line: DmLine = {
        id: `${p.turnId}:dm`,
        turnId: p.turnId,
        role: "dm",
        content,
        adjudication: p.adjudication,
        debug: p.debug,
        combatOutcome: r.combat_state?.ended ? r.combat_state.outcome : undefined,
        isTurn: true,
      };
      return { lines: [...state.lines, line], pending: null, announcement: content };
    }
    case "fail": {
      if (action.reason === "ended") {
        return {
          lines: [
            ...state.lines.filter((l) => l.turnId !== p.turnId),
            { id: `${p.turnId}:notice`, role: "notice", content: CAMPAIGN_ENDED_TEXT },
          ],
          pending: null,
          announcement: CAMPAIGN_ENDED_TEXT,
        };
      }
      const lines: Line[] = state.lines.map((l) =>
        l.turnId === p.turnId && l.role === "user" ? { ...l, failed: true } : l,
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
        });
      }
      lines.push({
        id: `${p.turnId}:error`,
        turnId: p.turnId,
        role: "error",
        content: action.message,
        code: action.code,
        retryInput: p.input,
      });
      return { lines, pending: null, announcement: action.message };
    }
  }
  return state;
}
