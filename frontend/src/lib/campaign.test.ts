import { expect, it } from "vitest";

import type { AdventureRead, CampaignRead } from "../api/types";
import { adventureTitleForDisplay, campaignDisplayName } from "./campaign";

const campaign = (title: string, slug: string): Pick<CampaignRead, "title" | "adventure_collections"> => ({
  title, adventure_collections: [slug],
});
const adventure = (title: string, slug: string): Pick<AdventureRead, "title" | "slug"> => ({ title, slug });

it("names a failed generation without its in-progress placeholder", () => {
  const slug = "ai-campaign-20261018";
  expect(campaignDisplayName(campaign("AI campaign (generating…)", slug), adventure("AI campaign (generating…)", slug)))
    .toBe("AI campaign");
});

it("uses the stored campaign title instead of a title derived from the slug", () => {
  expect(campaignDisplayName(campaign("Lost Mine of Phandelver", "d_d_5e_lost_mine"), adventure("D D 5E Lost Mine", "d_d_5e_lost_mine")))
    .toBe("Lost Mine of Phandelver");
});

it("uses the adventure's stored title over a generic campaign title, and never a placeholder", () => {
  expect(campaignDisplayName(campaign("Solo Session", "the_salt_gate"), adventure("The Salt Gate", "the_salt_gate")))
    .toBe("The Salt Gate");
});

it("keeps a real stored title and falls back to the slug only for generic titles", () => {
  expect(campaignDisplayName(campaign("Untitled campaign", "old_road"), null)).toBe("Old Road");
  expect(campaignDisplayName(campaign("Chalice Of The Mountain God", "chalice"), null)).toBe("Chalice Of The Mountain God");
});

it("hides the slug-derived subtitle that duplicates nothing useful", () => {
  expect(adventureTitleForDisplay(adventure("D D 5E Lost Mine", "d_d_5e_lost_mine"))).toBeNull();
  expect(adventureTitleForDisplay(adventure("The Salt Gate (generating…)", "the_salt_gate"))).toBeNull();
  expect(adventureTitleForDisplay(adventure("Lost Mine of Phandelver", "d_d_5e_lost_mine"))).toBe("Lost Mine of Phandelver");
});
