import type { AdventureRead, CampaignRead, SessionRead } from "../api/types";
import { campaignDisplayName } from "./campaign";

export interface CardModel {
  adventure: AdventureRead;
  name: string;
  /** Latest campaign row for this module (active preferred). */
  campaign: CampaignRead | null;
  state: "processing" | "failed" | "ready" | "ended";
  lastPlayed: string | null;
}

export function buildCards(
  adventures: AdventureRead[],
  campaigns: CampaignRead[] | undefined,
  sessions: SessionRead[] | undefined,
  userId: number,
): CardModel[] {
  const lastByCampaign = new Map<number, string>();
  for (const s of sessions ?? []) {
    const prev = lastByCampaign.get(s.campaign_id);
    if (!prev || s.started_at > prev) lastByCampaign.set(s.campaign_id, s.started_at);
  }
  const cards = adventures.map((adventure): CardModel => {
    const mine = (campaigns ?? [])
      .filter((c) => c.created_by === userId && c.adventure_collections.includes(adventure.slug))
      .sort((a, b) => b.created_at.localeCompare(a.created_at));
    const campaign = mine.find((c) => c.status === "active") ?? mine[0] ?? null;
    const lastPlayed =
      mine
        .map((c) => lastByCampaign.get(c.id))
        .filter((x): x is string => !!x)
        .sort()
        .at(-1) ?? null;
    const state =
      adventure.status === "processing"
        ? "processing"
        : adventure.status === "failed"
          ? "failed"
          : campaign?.status === "ended"
            ? "ended"
            : "ready";
    const name = campaign
      ? campaignDisplayName(campaign, adventure)
      : adventure.title;
    return { adventure, name, campaign, state, lastPlayed };
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
