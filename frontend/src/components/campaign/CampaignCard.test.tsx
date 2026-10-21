// @vitest-environment jsdom
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";
import type { AdventureRead, CampaignRead, SessionRead } from "../../api/types";
import { CampaignCard } from "./CampaignCard";
const adventure: AdventureRead = {
  slug: "sheep",
  title: "The Wild Sheep Chase",
  description: "A talking sheep seeks help.",
  cover_image: null,
  level_range: "4–5",
  library_archived: true,
};
const campaign: CampaignRead = {
  id: 7,
  title: "Friday party",
  system: "5e",
  created_by: 1,
  created_at: "2026-10-21",
  adventure_collections: ["sheep"],
  status: "active",
  ended_at: null,
  characters: [
    {
      assignment_id: 1,
      character_id: 10,
      name: "Rowan",
      character_class: "Wizard",
      level: 1,
      hp_current: 6,
      hp_max: 6,
    },
    {
      assignment_id: 2,
      character_id: 11,
      name: "Eda",
      character_class: "Fighter",
      level: 2,
      hp_current: 12,
      hp_max: 20,
    },
  ],
};
function html(c = campaign, sessions?: SessionRead[]) {
  return renderToStaticMarkup(
    <MemoryRouter>
      <CampaignCard campaign={c} adventure={adventure} sessions={sessions} />
    </MemoryRouter>,
  );
}
it("shows the campaign identity, adventure and every character even when the adventure is archived", () => {
  const view = html();
  expect(view).toContain("Friday party");
  expect(view).toContain("The Wild Sheep Chase");
  expect(view).toContain("Rowan");
  expect(view).toContain("Eda");
  expect(view).toContain("12/20");
  expect(view).toContain('href="/adventures/sheep"');
  expect(view).toContain('href="/campaigns/7"');
});
it("keeps different instances of one adventure as separate campaign cards", () => {
  expect(html()).toContain("Campaign #7");
  const other = html({
    ...campaign,
    id: 8,
    title: "Saturday party",
    characters: [],
  });
  expect(other).toContain("Campaign #8");
  expect(other).not.toContain("Rowan");
});
it("never starts a new session when campaign readiness is blocked", () => {
  const view = html({
    ...campaign,
    readiness: {
      play_allowed: false,
      history_allowed: true,
      state: "source_missing",
      primary_action: "recover_source",
      blockers: [],
    },
  });
  expect(view).toContain("Open campaign");
  expect(view).not.toContain("Start session");
});
