// @vitest-environment jsdom
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import type { CampaignRead } from "../api/types";
import Home from "./Home";
const data = vi.hoisted(() => ({ campaigns: [] as unknown[], campaignError: null as unknown }));
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));
vi.mock("../lib/useQuery", () => ({ useQuery: (key: string) => ({ data: key === "me" ? { id: 1, email: "player@example.test", display_name: "Player" } : key === "campaigns" ? (data.campaignError ? undefined : data.campaigns) : [], error: key === "campaigns" ? data.campaignError : null, loading: false, refetch: vi.fn(), setData: vi.fn() }) }));
it("shows orphan campaigns when the adventure library is completely empty", () => {
  const campaign: CampaignRead = { id: 2, title: "Lost module", system: "5e", created_by: 1, created_at: "2026-10-14", adventure_collections: ["lost_module"], status: "active", ended_at: null, source_mode: "module", retrieval_degraded: true, play_allowed: false, sources: [{ slug: "lost_module", title: "Lost module", status: "missing", usable_chunks: 0, expected_chunks: null, collection_exists: false, source_pdf_available: false, message: "Missing source" }] };
  data.campaigns = [campaign]; data.campaignError = null;
  const html = renderToStaticMarkup(<MemoryRouter><Home /></MemoryRouter>);
  expect(html).toContain("Lost module"); expect(html).toContain("Recover source"); expect(html).not.toContain("No campaigns yet");
});
it("surfaces campaign loading failures instead of claiming library is empty", () => {
  data.campaigns = []; data.campaignError = new Error("Campaign sources unavailable");
  const html = renderToStaticMarkup(<MemoryRouter><Home /></MemoryRouter>);
  expect(html).toContain("Something went wrong while processing this action.");
  expect(html).not.toContain("Campaign sources unavailable");
  expect(html).not.toContain("No campaigns yet");
});
