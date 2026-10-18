import type { AdventureRead, CampaignRead } from "../api/types";

/** Titles the backend/seed use when nobody named the campaign. */
const GENERIC_TITLES = new Set(["", "solo session", "new campaign", "untitled campaign", "ai campaign"]);

/** The placeholder a generation attempt carries while it runs, and keeps if it never finishes. */
const GENERATING_SUFFIX = /\s*\(generating(?:…|\.\.\.)\)\s*$/i;

function slugTitle(slug: string): string {
  return slug.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Drop the generation placeholder so a failed attempt never reads as still in progress. */
export function stripGenerationPlaceholder(title: string): string {
  return title.replace(GENERATING_SUFFIX, "").trim();
}

/** Is this title only the slug in another spelling (for example "D D 5e Lost Mine" for "d_d_5e_lost_mine")? */
export function isSlugDerivedTitle(title: string, slug: string): boolean {
  const compact = (value: string) => value.replace(/[\s_-]+/g, "").toLowerCase();
  return compact(title) === compact(slug);
}

/** The adventure's stored title when it is a real name, or null when only the slug or placeholder is left. */
export function adventureTitleForDisplay(adventure: Pick<AdventureRead, "title" | "slug">): string | null {
  const title = stripGenerationPlaceholder(adventure.title);
  return title && !isSlugDerivedTitle(title, adventure.slug) ? title : null;
}

/** Is this campaign title a placeholder (seed default or derived from the slug)? */
export function isGenericCampaignTitle(campaign: Pick<CampaignRead, "title" | "adventure_collections">) {
  const t = stripGenerationPlaceholder(campaign.title).toLowerCase();
  if (GENERIC_TITLES.has(t)) return true;
  return campaign.adventure_collections.some((slug) => slug.toLowerCase() === t);
}

/**
 * The name players see for a campaign, used identically on /campaigns and the campaign hub:
 * the stored campaign title when it is a real name, else the adventure's stored title, else a
 * title from the slug. Placeholders never win.
 */
export function campaignDisplayName(
  campaign: Pick<CampaignRead, "title" | "adventure_collections">,
  adventure?: Pick<AdventureRead, "title"> | null,
): string {
  const stored = stripGenerationPlaceholder(campaign.title);
  if (stored && !isGenericCampaignTitle(campaign)) return stored;
  const adventureTitle = adventure ? stripGenerationPlaceholder(adventure.title) : "";
  if (adventureTitle) return adventureTitle;
  const slug = campaign.adventure_collections[0];
  return slug ? slugTitle(slug) : "Untitled campaign";
}
