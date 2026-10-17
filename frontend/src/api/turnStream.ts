import { readSse } from "./sse";
import type { AdjudicationResult, DebugPayload, TurnResult } from "./types";

export interface TurnStreamHandlers {
  onStatus?: (stage: string) => void;
  onToken?: (text: string) => void;
  onAdjudication?: (result: AdjudicationResult) => void;
  /** Fires once per turn (only when debug=true was requested), just before done. */
  onDebug?: (payload: DebugPayload) => void;
  /** Any bytes received (events or keep-alive comments) — used to feed a watchdog. */
  onActivity?: () => void;
}

export interface TurnError {
  message: string;
  code?: string;
  category?: SafeErrorCategory;
  recoveryAction?: SafeRecoveryAction;
  retryable?: boolean;
  outcome?: "not_committed" | "committed" | "unknown";
  supportRef?: string;
}

export type SafeErrorCategory =
  | "source" | "review" | "validation" | "assignment" | "busy"
  | "provider_unavailable" | "rate_limited" | "quota_exceeded"
  | "provider_configuration" | "cancelled" | "interrupted" | "internal"
  | "not_found";

export type SafeRecoveryAction =
  | "repair_source" | "review_source" | "fix_input" | "assign_character"
  | "wait" | "retry" | "check_existing_result" | "configure_provider"
  | "restart_preparation" | "contact_support" | "read_history";

const SAFE_CODES: Record<string, { category: SafeErrorCategory; message: string; action: SafeRecoveryAction }> = {
  campaign_sources_unavailable: { category: "source", message: "The adventure source needs attention before play can continue.", action: "repair_source" },
  source_missing: { category: "source", message: "The adventure source needs attention before play can continue.", action: "repair_source" },
  source_unavailable: { category: "source", message: "The adventure source needs attention before play can continue.", action: "repair_source" },
  campaign_source_unavailable: { category: "source", message: "The adventure source needs attention before play can continue.", action: "repair_source" },
  required_books_unavailable: { category: "source", message: "The adventure source needs attention before play can continue.", action: "repair_source" },
  required_book_unavailable: { category: "source", message: "The adventure source needs attention before play can continue.", action: "repair_source" },
  required_book_phb_unindexed: { category: "source", message: "The Player's Handbook has no exact indexed version for this rules edition. Re-ingest that rulebook for this edition, then start a new campaign.", action: "restart_preparation" },
  required_book_dmg_unindexed: { category: "source", message: "The DM's Guide has no exact indexed version for this rules edition. Re-ingest that rulebook for this edition, then start a new campaign.", action: "restart_preparation" },
  required_book_mm_unindexed: { category: "source", message: "The Monster Manual has no exact indexed version for this rules edition. Re-ingest that rulebook for this edition, then start a new campaign.", action: "restart_preparation" },
  required_books_missing: { category: "source", message: "The adventure source needs attention before play can continue.", action: "repair_source" },
  book_version_unavailable: { category: "source", message: "The adventure source needs attention before play can continue.", action: "repair_source" },
  required_assets_unavailable: { category: "source", message: "The adventure source needs attention before play can continue.", action: "repair_source" },
  preparation_source_invalid: { category: "source", message: "The adventure source needs attention before play can continue.", action: "repair_source" },
  preparation_failed: { category: "source", message: "Adventure preparation needs attention. Review its setup and restart preparation to continue.", action: "restart_preparation" },
  adventure_manifest_review_required: { category: "review", message: "Review the current adventure material before continuing.", action: "review_source" },
  manifest_review_required: { category: "review", message: "Review the current adventure material before continuing.", action: "review_source" },
  review_required: { category: "review", message: "Review the current adventure material before continuing.", action: "review_source" },
  source_review_required: { category: "review", message: "Review the current adventure material before continuing.", action: "review_source" },
  source_review_needed: { category: "review", message: "Review the current adventure material before continuing.", action: "review_source" },
  adventure_review_needed: { category: "review", message: "Review the current adventure material before continuing.", action: "review_source" },
  saved_version_conflict: { category: "review", message: "The saved adventure version needs review before continuing.", action: "review_source" },
  preparation_in_progress: { category: "busy", message: "Preparation is still running. Check its status before trying again.", action: "wait" },
  preparation_busy: { category: "busy", message: "Preparation is still running. Check its status before trying again.", action: "wait" },
  busy: { category: "busy", message: "This action is still being processed. Check save status before trying again.", action: "wait" },
  request_busy: { category: "busy", message: "This action is still being processed. Check save status before trying again.", action: "wait" },
  lock_conflict: { category: "busy", message: "This action is still being processed. Check save status before trying again.", action: "wait" },
  session_busy: { category: "busy", message: "This action is still being processed. Check save status before trying again.", action: "wait" },
  turn_busy: { category: "busy", message: "This action is still being processed. Check save status before trying again.", action: "wait" },
  validation_failed: { category: "validation", message: "Check the highlighted information and correct it before continuing.", action: "fix_input" },
  invalid_request: { category: "validation", message: "Check the highlighted information and correct it before continuing.", action: "fix_input" },
  stage_validation_failed: { category: "validation", message: "Check the highlighted information and correct it before continuing.", action: "fix_input" },
  unsupported_edition: { category: "validation", message: "Choose a rules edition supported by this adventure.", action: "fix_input" },
  unsupported_party: { category: "validation", message: "Check the party information and correct it before continuing.", action: "fix_input" },
  unsupported_progression: { category: "validation", message: "Check the progression information and correct it before continuing.", action: "fix_input" },
  unsupported_hero_capability: { category: "validation", message: "Check the hero information and correct it before continuing.", action: "fix_input" },
  creature_identity_unresolved: { category: "validation", message: "Choose an exact verified creature name before starting the encounter.", action: "fix_input" },
  preparation_setup_required: { category: "validation", message: "Choose a hero and preparation budget before continuing.", action: "fix_input" },
  preparation_setup_invalid: { category: "validation", message: "Correct the preparation setup before continuing.", action: "fix_input" },
  preparation_profile_invalid: { category: "provider_configuration", message: "The preparation service needs configuration before it can continue.", action: "configure_provider" },
  rules_edition_mismatch: { category: "validation", message: "Choose a rules edition supported by this adventure.", action: "fix_input" },
  character_not_assigned: { category: "assignment", message: "Choose a character assigned to this campaign before continuing.", action: "assign_character" },
  assignment_required: { category: "assignment", message: "Choose a character assigned to this campaign before continuing.", action: "assign_character" },
  character_assignment_required: { category: "assignment", message: "Choose a character assigned to this campaign before continuing.", action: "assign_character" },
  assignment_conflict: { category: "assignment", message: "Choose a character assigned to this campaign before continuing.", action: "assign_character" },
  choose_character: { category: "assignment", message: "Choose a character assigned to this campaign before continuing.", action: "assign_character" },
  character_assignment_conflict: { category: "assignment", message: "Choose a character assigned to this campaign before continuing.", action: "assign_character" },
  provider_unavailable: { category: "provider_unavailable", message: "The Dungeon Master service could not complete this action. Check save status before retrying.", action: "check_existing_result" },
  llm_unavailable: { category: "provider_unavailable", message: "The Dungeon Master service could not complete this action. Check save status before retrying.", action: "check_existing_result" },
  rate_limited: { category: "rate_limited", message: "The Dungeon Master service is receiving too many requests. Check save status before retrying.", action: "check_existing_result" },
  quota_exceeded: { category: "quota_exceeded", message: "The configured service allowance has been reached. Review the provider setup before continuing.", action: "configure_provider" },
  provider_configuration: { category: "provider_configuration", message: "The Dungeon Master service needs configuration before it can continue.", action: "configure_provider" },
  cancelled: { category: "cancelled", message: "Preparation was cancelled. Your saved work is retained.", action: "restart_preparation" },
  interrupted: { category: "interrupted", message: "The connection stopped before the result was confirmed. Check the saved result before retrying.", action: "check_existing_result" },
  incomplete: { category: "interrupted", message: "The connection stopped before the result was confirmed. Check the saved result before retrying.", action: "check_existing_result" },
  turn_not_found: { category: "interrupted", message: "The server confirms this action was not saved. You can retry it safely.", action: "retry" },
  turn_save_failed: { category: "internal", message: "The turn could not be saved. Check the saved result before trying again.", action: "check_existing_result" },
  turn_failed: { category: "internal", message: "Something went wrong while processing this action. Check the saved result before trying again.", action: "contact_support" },
  internal_fault: { category: "internal", message: "Something went wrong while processing this action. Check the saved result before trying again.", action: "contact_support" },
  readiness_unavailable: { category: "internal", message: "Campaign readiness could not be confirmed. Check the saved campaign status before continuing.", action: "check_existing_result" },
  campaign_ended: { category: "validation", message: "This campaign is complete. You can read its saved history.", action: "read_history" },
  completed: { category: "validation", message: "This campaign is complete. You can read its saved history.", action: "read_history" },
  turn_identity_conflict: { category: "validation", message: "This action identifier already belongs to a different action. Check its saved result before continuing.", action: "check_existing_result" },
  not_found: { category: "not_found", message: "That saved item could not be found. Refresh and check that it is still available.", action: "fix_input" },
};

const SAFE_CATEGORIES = new Set<SafeErrorCategory>([
  "source", "review", "validation", "assignment", "busy", "provider_unavailable",
  "rate_limited", "quota_exceeded", "provider_configuration", "cancelled", "interrupted",
  "internal", "not_found",
]);
const SAFE_ACTIONS = new Set<SafeRecoveryAction>([
  "repair_source", "review_source", "fix_input", "assign_character", "wait", "retry",
  "check_existing_result", "configure_provider", "restart_preparation", "contact_support", "read_history",
]);

export const GENERIC_SAFE_ERROR = "Something went wrong while processing this action. Check its saved status before trying again.";

function safeSupportRef(value: unknown): string | undefined {
  return typeof value === "string" && /^err-[a-f0-9]{16}$/.test(value) ? value : undefined;
}

/** Parse a server error into fixed player copy; free-form details never reach the UI. */
export function safeTurnError(payload: unknown, fallback = GENERIC_SAFE_ERROR): TurnError {
  if (typeof payload === "string") {
    const trimmed = payload.trim();
    if (trimmed) {
      try { return safeTurnError(JSON.parse(trimmed) as unknown, fallback); }
      catch { return { message: fallback, code: "turn_failed", category: "internal", recoveryAction: "contact_support" }; }
    }
    return { message: fallback, code: "turn_failed", category: "internal", recoveryAction: "contact_support" };
  }
  if (payload && typeof payload === "object") {
    const p = payload as Record<string, unknown>;
    const detail = p.detail && typeof p.detail === "object" && !Array.isArray(p.detail)
      ? p.detail as Record<string, unknown> : {};
    const code = typeof p.code === "string" && SAFE_CODES[p.code]
      ? p.code
      : typeof detail.code === "string" && SAFE_CODES[detail.code] ? detail.code : undefined;
    if (code) {
      const mapped = SAFE_CODES[code];
      const categoryValue = p.category ?? detail.category;
      const actionValue = p.recovery_action ?? detail.recovery_action;
      const outcomeValue = p.outcome ?? detail.outcome;
      const retryableValue = p.retryable ?? detail.retryable;
      const supportRefValue = p.support_ref ?? detail.support_ref;
      const category = SAFE_CATEGORIES.has(categoryValue as SafeErrorCategory)
        ? categoryValue as SafeErrorCategory : mapped.category;
      const recoveryAction = SAFE_ACTIONS.has(actionValue as SafeRecoveryAction)
        ? actionValue as SafeRecoveryAction : mapped.action;
      const outcome = outcomeValue === "not_committed" || outcomeValue === "committed" || outcomeValue === "unknown"
        ? outcomeValue : undefined;
      const retryable = retryableValue === true && recoveryAction === "retry" && outcome === "not_committed";
      return {
        code, category, recoveryAction, message: mapped.message,
        retryable, outcome, supportRef: safeSupportRef(supportRefValue),
      };
    }
  }
  return { message: fallback, code: "turn_failed", category: "internal", recoveryAction: "contact_support" };
}

export function safeFailureFromUnknown(error: unknown): TurnError {
  if (error && typeof error === "object") {
    const candidate = error as Record<string, unknown>;
    if (typeof candidate.body === "string") {
      const parsed = safeTurnError(candidate.body);
      if (parsed.code !== "turn_failed" || parsed.supportRef) return parsed;
    }
    if (candidate.status === 0 || error instanceof TypeError) {
      return { message: "Can't reach the Edgar server. Check your connection and saved status before trying again.", code: "interrupted", category: "interrupted", recoveryAction: "check_existing_result", outcome: "unknown" };
    }
  }
  return safeTurnError(error);
}

/** How a turn stream ended. `incomplete` = the connection closed with no terminal event. */
export type TurnStreamOutcome =
  | { kind: "done"; result: TurnResult }
  | { kind: "error"; error: TurnError }
  | { kind: "incomplete" };

/**
 * Normalize sync/SSE error payload shapes without displaying provider or exception text.
 */
export function parseTurnError(payload: unknown, fallback = "Something went wrong."): TurnError {
  return safeTurnError(payload, fallback);
}

/**
 * Read the turn SSE stream to its terminal event, dispatching intermediate events to handlers.
 * Never silently swallows a premature end: returns `{kind: "incomplete"}` instead.
 * Network/abort errors propagate as exceptions.
 */
export async function consumeTurnStream(
  response: Response,
  handlers: TurnStreamHandlers,
): Promise<TurnStreamOutcome> {
  for await (const { event, data } of readSse(response, { onActivity: handlers.onActivity })) {
    if (event === "status") {
      handlers.onStatus?.(typeof data.stage === "string" ? data.stage : "");
    } else if (event === "token") {
      if (typeof data.text === "string" && data.text) handlers.onToken?.(data.text);
    } else if (event === "adjudication") {
      handlers.onAdjudication?.(data as unknown as AdjudicationResult);
    } else if (event === "debug") {
      handlers.onDebug?.(data as unknown as DebugPayload);
    } else if (event === "done") {
      return { kind: "done", result: data as TurnResult };
    } else if (event === "error") {
      return { kind: "error", error: parseTurnError(data, "The turn could not be completed.") };
    }
  }
  return { kind: "incomplete" };
}
