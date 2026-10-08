import type { AdventureRead, CampaignRead } from "../api/types";

/** Titles the backend/seed use when nobody named the campaign. */
const GENERIC_TITLES = new Set(["", "solo session", "new campaign", "untitled campaign"]);

function slugTitle(slug: string): string {
  return slug.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Is this campaign title a placeholder (seed default or derived from the slug)? */
export function isGenericCampaignTitle(campaign: Pick<CampaignRead, "title" | "adventure_collections">) {
  const t = campaign.title.trim().toLowerCase();
  if (GENERIC_TITLES.has(t)) return true;
  return campaign.adventure_collections.some((slug) => slug.toLowerCase() === t);
}

/**
 * The name players see for a campaign, used identically on /campaigns and the campaign hub:
 * the adventure's title when the campaign title is a generic default, else the campaign title.
 */
export function campaignDisplayName(
  campaign: Pick<CampaignRead, "title" | "adventure_collections">,
  adventure?: Pick<AdventureRead, "title"> | null,
): string {
  if (!isGenericCampaignTitle(campaign)) return campaign.title;
  if (adventure?.title) return adventure.title;
  const slug = campaign.adventure_collections[0];
  return slug ? slugTitle(slug) : campaign.title || "Untitled campaign";
}
