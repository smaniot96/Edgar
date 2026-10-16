// @vitest-environment jsdom
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import type { CampaignRead } from "../api/types";
import Home from "./Home";
const data = vi.hoisted(() => ({ campaigns: [] as unknown[], sessions: [] as unknown[], campaignError: null as unknown }));
beforeEach(() => { data.campaigns = []; data.sessions = []; data.campaignError = null; });
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));
vi.mock("../lib/useQuery", () => ({ useQuery: (key: string) => ({ data: key === "me" ? { id: 1, email: "player@example.test", display_name: "Player" } : key === "campaigns" ? (data.campaignError ? undefined : data.campaigns) : key === "sessions" ? data.sessions : [], error: key === "campaigns" ? data.campaignError : null, loading: false, refetch: vi.fn(), setData: vi.fn() }) }));
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

it("identifies the selected playable save and hero beside Resume even when a newer save is blocked", () => {
  data.campaigns = [1, 2].map((id) => ({ id, title: id === 1 ? "Blocked crossing" : "Saved trail", created_by: 1, created_at: "2026-10-14", adventure_collections: [], source_mode: "homebrew", status: "active" }));
  data.sessions = [
    { id: 9, campaign_id: 1, has_history: true, last_played_at: "2026-10-16", character_name: "Other hero", readiness: { play_allowed: false, history_allowed: true } },
    { id: 7, campaign_id: 2, has_history: true, last_played_at: "2026-10-15", character_name: "Mara Venn", readiness: { play_allowed: true, history_allowed: true } },
  ];
  const html = renderToStaticMarkup(<MemoryRouter><Home /></MemoryRouter>);
  const rendered = document.createElement("div"); rendered.innerHTML = html;
  const resume = rendered.querySelector('button[aria-describedby="resume-save-identity"]')!;
  expect(resume.textContent).toContain("Resume: Saved trail");
  expect(rendered.querySelector("#resume-save-identity")?.textContent).toBe("Mara Venn · Save #7");
  expect(resume.textContent).not.toContain("Blocked crossing");
});
