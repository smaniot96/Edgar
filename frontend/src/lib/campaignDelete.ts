import { deleteAdventure, deleteCampaign, deleteCampaignAndUnusedSources } from "../api/client";
import type { CampaignRead, SessionRead, SourceReleaseOutcome } from "../api/types";

export interface DeleteSource {
  slug: string;
  title: string;
  /** The source record is already gone; there is nothing left to delete. */
  missing?: boolean;
}

/** What a delete action removes (EDG-055 F16). */
export interface DeleteTarget {
  /** What the player calls it on the card or page. */
  name: string;
  /** The player's campaigns to delete. Empty when the card is only an adventure source. */
  campaigns: CampaignRead[];
  /** Adventure sources bound to those campaigns (or the card's own source). */
  sources: DeleteSource[];
}

const KEPT_BECAUSE: Record<Exclude<SourceReleaseOutcome, "removed" | "missing">, string> = {
  in_use: "another campaign still uses it",
  built_in: "it is a bundled adventure",
  protected: "it is not an uploaded adventure",
  not_permitted: "only an administrator can delete adventure sources",
  processing: "it is still being prepared",
  retained: "this prepared version is kept for saved games",
  unavailable: "the search index could not be reached, so nothing was removed from it",
};

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export function savesText(campaigns: CampaignRead[], sessions: SessionRead[] | undefined): string {
  const many = campaigns.length > 1;
  if (!sessions) return many ? "all their saved sessions" : "all its saved sessions";
  const ids = new Set(campaigns.map((c) => c.id));
  const count = sessions.filter((s) => ids.has(s.campaign_id)).length;
  if (count === 0) return many ? "their settings (there are no saved sessions)" : "its settings (it has no saved sessions)";
  return `${many ? "their" : "its"} ${plural(count, "saved session")}`;
}

/** Player-facing summary of what a confirmed delete did. */
export function deletedMessage(target: DeleteTarget, outcomes: Map<string, SourceReleaseOutcome>): string {
  if (target.campaigns.length === 0) return `Deleted the adventure “${target.name}”.`;
  const lines = [`Deleted “${target.name}” and its saved sessions.`];
  for (const source of target.sources) {
    const outcome = outcomes.get(source.slug);
    if (outcome === "removed") lines.push(`The adventure source “${source.title}” was deleted too.`);
    else if (outcome && outcome !== "missing") {
      lines.push(`The adventure source “${source.title}” was kept because ${KEPT_BECAUSE[outcome]}.`);
    }
  }
  return lines.join(" ");
}

/** Delete each campaign in turn; a shared source is released by the last one, so later outcomes win. */
export async function deleteTarget(target: DeleteTarget): Promise<Map<string, SourceReleaseOutcome>> {
  const outcomes = new Map<string, SourceReleaseOutcome>();
  if (target.campaigns.length === 0) {
    for (const source of target.sources) {
      await deleteAdventure(source.slug);
      outcomes.set(source.slug, "removed");
    }
    return outcomes;
  }
  for (const campaign of target.campaigns) {
    if (target.sources.length === 0) {
      await deleteCampaign(campaign.id);
      continue;
    }
    const result = await deleteCampaignAndUnusedSources(campaign.id);
    for (const source of result.sources) outcomes.set(source.slug, source.outcome);
  }
  return outcomes;
}
