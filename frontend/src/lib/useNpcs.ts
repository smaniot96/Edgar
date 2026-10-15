import { listNpcs } from "../api/client";
import { useQuery } from "./useQuery";

/** Keep manual notes and each saved playthrough in separate abortable cache entries. */
export function useNpcs(campaignId: number, sessionId: number | null, enabled = true) {
  const contextKey = sessionId == null ? "campaign-notes" : `session-${sessionId}`;
  return useQuery(enabled ? `npcs:${campaignId}:${contextKey}` : null, (signal) =>
    listNpcs(campaignId, sessionId, signal),
  );
}
