import { Link } from "react-router-dom";

import type { AdventureRead, CampaignRead, SessionRead } from "../../api/types";
import { campaignDisplayName } from "../../lib/campaign";
import { selectCampaignSession } from "../../lib/campaignCards";
import { relativeTime } from "../../lib/format";
import { rememberLastSession } from "../../lib/lastSession";
import { Pill } from "../Pill";
import { ButtonLink } from "../ui/Button";
import { AdventureCover } from "./AdventureCover";

/** One card per playthrough, even when several use the same adventure. */
export function CampaignCard({
  campaign,
  adventure,
  sessions,
}: {
  campaign: CampaignRead;
  adventure?: AdventureRead;
  sessions?: SessionRead[];
}) {
  const name = campaignDisplayName(campaign, adventure);
  const saved = selectCampaignSession(sessions, campaign.id);
  const readable =
    saved &&
    (saved.readiness?.play_allowed === true ||
      (saved.has_history && saved.readiness?.history_allowed === true));
  const characters = campaign.characters;
  return (
    <article className="flex min-w-0 flex-col overflow-hidden rounded-card border border-line bg-surface-sunken">
      <div className="[&>div]:h-28 [&>img]:h-28">
        <AdventureCover
          coverImage={adventure?.cover_image}
          name={adventure?.title || name}
        />
      </div>
      <div className="flex flex-1 flex-col gap-4 p-5">
        <div className="flex flex-wrap items-center gap-2">
          <Pill tone={campaign.status === "active" ? "success" : "neutral"}>
            {campaign.status === "active" ? "Active" : "Ended"}
          </Pill>
          <span className="text-xs text-ink-subtle">
            Campaign #{campaign.id}
          </span>
        </div>
        <div>
          <h2 className="break-words font-display text-xl">
            <Link
              to={`/campaigns/${campaign.id}`}
              className="rounded-sm hover:text-ember-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400"
            >
              {name}
            </Link>
          </h2>
          <p className="mt-2 text-xs text-ink-subtle">ADVENTURE</p>
          {campaign.adventure_collections.length === 1 ? (
            <Link
              to={`/adventures/${encodeURIComponent(campaign.adventure_collections[0])}`}
              className="mt-1 block break-words text-sm text-ember-300 hover:underline"
            >
              {adventure?.title ||
                campaign.sources?.[0]?.title ||
                campaign.adventure_collections[0].replace(/_/g, " ")}
            </Link>
          ) : (
            <p className="mt-1 text-sm text-ink-muted">
              {campaign.source_mode === "homebrew"
                ? "Homebrew story"
                : "Legacy adventure bindings"}
            </p>
          )}
        </div>
        <div>
          <h3 className="mb-2 text-xs text-ink-subtle">
            PARTY{characters?.length ? ` · ${characters.length}` : ""}
          </h3>
          {characters === undefined ? (
            <p className="text-sm text-ink-muted">
              Open campaign to view characters.
            </p>
          ) : characters.length === 0 ? (
            <p className="text-sm text-ink-muted">
              {campaign.status === "ended"
                ? "Characters released when the campaign ended."
                : "No characters assigned yet."}
            </p>
          ) : (
            <ul className="space-y-2">
              {characters.map((character) => (
                <li
                  key={character.assignment_id}
                  className="flex items-center gap-3"
                >
                  <span
                    aria-hidden
                    className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-ember-800/50 bg-ember-950/50 font-display text-ember-200"
                  >
                    {character.name.charAt(0)}
                  </span>
                  <div className="min-w-0">
                    <p className="break-words text-sm font-medium">
                      {character.name}
                    </p>
                    <p className="text-xs text-ink-muted">
                      Level {character.level} {character.character_class} · HP{" "}
                      {character.hp_current}/{character.hp_max}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="mt-auto space-y-3 border-t border-line pt-4">
          <p className="text-xs text-ink-muted">
            {saved?.has_history && saved.last_played_at
              ? `Last played ${relativeTime(saved.last_played_at)}`
              : "First session awaits"}
          </p>
          <div className="flex flex-wrap gap-2">
            {readable ? (
              <ButtonLink
                to={`/play/${saved.id}`}
                onClick={() => rememberLastSession(saved.id)}
              >
                {saved.readiness?.play_allowed ? "Continue" : "Read story"}
              </ButtonLink>
            ) : campaign.status === "active" &&
              campaign.readiness?.play_allowed === true ? (
              <ButtonLink to={`/campaigns/${campaign.id}/setup`}>
                {characters?.length ? "Start session" : "Choose characters"}
              </ButtonLink>
            ) : null}
            <ButtonLink variant="secondary" to={`/campaigns/${campaign.id}`}>
              Open campaign
            </ButtonLink>
          </div>
        </div>
      </div>
    </article>
  );
}
