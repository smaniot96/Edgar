import type { AdventureRead, CampaignRead, ReadinessRead, SessionRead } from "../api/types";
import { adventureTitleForDisplay, campaignDisplayName, stripGenerationPlaceholder } from "./campaign";

export interface CardModel {
  adventure: AdventureRead;
  name: string;
  /** Latest campaign row for this module (active preferred). */
  campaign: CampaignRead | null;
  /** Every campaign of this player the card stands for (an active one and any ended ones). */
  campaigns: CampaignRead[];
  state: "processing" | "failed" | "ready" | "ended" | "recovery";
  lastPlayed: string | null;
  /** Most useful saved playthrough for the card's primary action, when one exists. */
  primarySession: SessionRead | null;
  readiness: ReadinessRead | null;
}

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

export function buildCards(
  adventures: AdventureRead[],
  campaigns: CampaignRead[] | undefined,
  sessions: SessionRead[] | undefined,
  userId: number,
  includeArchived = false,
): CardModel[] {
  const lastByCampaign = new Map<number, string>();
  for (const s of sessions ?? []) {
    if (!s.has_history) continue;
    const played = s.last_played_at ?? s.started_at;
    const prev = lastByCampaign.get(s.campaign_id);
    if (!prev || played > prev) lastByCampaign.set(s.campaign_id, played);
  }
  const modules = new Map(adventures.map((a) => [a.slug, a]));
  for (const campaign of campaigns ?? []) {
    if (campaign.created_by !== userId) continue;
    for (const slug of campaign.adventure_collections) {
      if (!modules.has(slug)) {
        const source = campaign.sources?.find((s) => s.slug === slug);
        modules.set(slug, { slug, title: source?.title ?? campaign.title, description: null,
          level_range: null, cover_image: null, status: source?.status ?? "missing", error: source?.message });
      }
    }
    if (campaign.source_mode === "homebrew" && campaign.adventure_collections.length === 0) {
      modules.set(`homebrew_${campaign.id}`, { slug: `homebrew_${campaign.id}`, title: campaign.title,
        description: null, level_range: null, cover_image: null, status: "ready" });
    }
  }
  const cards = [...modules.values()]
    .filter((adventure) => includeArchived || !adventure.library_archived)
    .map((adventure): CardModel => {
    const mine = (campaigns ?? [])
      .filter((c) => c.created_by === userId && (c.adventure_collections.includes(adventure.slug) || (c.source_mode === "homebrew" && adventure.slug === `homebrew_${c.id}`)))
      .sort((a, b) => b.created_at.localeCompare(a.created_at));
    const campaign = mine.find((c) => c.status === "active") ?? mine[0] ?? null;
    const lastPlayed =
      mine
        .map((c) => lastByCampaign.get(c.id))
        .filter((x): x is string => !!x)
        .sort()
        .at(-1) ?? null;
    const primarySession = campaign ? selectCampaignSession(sessions, campaign.id) : null;
    const state =
      campaign?.source_mode === "homebrew" ? (campaign.status === "ended" ? "ended" : "ready")
      : campaign?.play_allowed === false ? "recovery"
      : adventure.status && !["processing", "failed", "ready"].includes(adventure.status) ? "recovery"
      : adventure.status === "processing"
        ? "processing"
        : adventure.status === "failed"
          ? "failed"
          : campaign?.status === "ended"
            ? "ended"
            : "ready";
    const name = campaign && (campaign.source_mode === "homebrew" || !adventureTitleForDisplay(adventure))
      ? campaignDisplayName(campaign, adventure)
      : stripGenerationPlaceholder(adventure.title);
    return { adventure, name, campaign, campaigns: mine, state, lastPlayed, primarySession, readiness: campaign?.readiness ?? adventure.readiness ?? null };
  });
  // Most recently played first; never-played keep API order after them.
  return cards
    .map((c, i) => ({ c, i }))
    .sort((a, b) => {
      const la = a.c.lastPlayed ?? "";
      const lb = b.c.lastPlayed ?? "";
      if (la !== lb) return lb.localeCompare(la);
      return a.i - b.i;
    })
    .map(({ c }) => c);
}
