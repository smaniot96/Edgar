import type { SessionRead } from "../api/types";

/** Choose a resumable transcript without letting a newer blocked/empty save hide it. */
export function selectResumeSession(sessions: SessionRead[] | undefined): SessionRead | null {
  const newest = (items: SessionRead[]) => [...items].sort((a, b) =>
    (b.last_played_at ?? b.started_at).localeCompare(a.last_played_at ?? a.started_at),
  )[0] ?? null;
  const history = (sessions ?? []).filter(
    (session) => session.has_history && session.readiness?.history_allowed !== false,
  );
  return newest(history.filter((session) => session.readiness?.play_allowed === true))
    ?? newest(history);
}

/** Select a campaign's best save: a playable session first, then readable saved history. */
export function selectCampaignSession(
  sessions: SessionRead[] | undefined,
  campaignId: number,
): SessionRead | null {
  const matching = (sessions ?? []).filter((session) => session.campaign_id === campaignId);
  const newest = (items: SessionRead[]) => [...items].sort((a, b) =>
    (b.last_played_at ?? b.started_at).localeCompare(a.last_played_at ?? a.started_at),
  )[0] ?? null;
  return newest(matching.filter((session) => session.has_history && session.readiness?.play_allowed === true))
    ?? newest(matching.filter((session) => session.readiness?.play_allowed === true))
    ?? newest(matching.filter((session) => session.has_history && session.readiness?.history_allowed === true));
}
