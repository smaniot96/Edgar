// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { AdventureRead, CampaignRead } from "../api/types";
import { installFakeApi, json } from "../test/fakeApi";
import Adventure from "./Adventure";
import Campaigns from "./Campaigns";

const ready = {
  play_allowed: true,
  history_allowed: true,
  state: "ready" as const,
  primary_action: "play" as const,
  blockers: [],
};
const adventure: AdventureRead = {
  slug: "sheep",
  title: "The Wild Sheep Chase",
  description: "A sheep asks for help.",
  level_range: "4–5",
  cover_image: null,
  status: "ready",
  readiness: ready,
  library_archived: true,
};
const campaign: CampaignRead = {
  id: 21,
  title: "Friday party",
  system: "5e",
  created_by: 1,
  created_at: "2026-10-21",
  adventure_collections: ["sheep"],
  status: "active",
  ended_at: null,
  readiness: ready,
  characters: [
    {
      assignment_id: 1,
      character_id: 11,
      name: "Rowan",
      character_class: "Wizard",
      level: 1,
      hp_current: 6,
      hp_max: 6,
    },
  ],
};
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  sessionStorage.clear();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  sessionStorage.clear();
  vi.unstubAllGlobals();
});
async function render(path: string, key = "initial") {
  await act(async () =>
    root.render(
      <MemoryRouter key={key} initialEntries={[path]}>
        <Routes>
          <Route path="/campaigns" element={<Campaigns />} />
          <Route path="/adventures/:slug" element={<Adventure />} />
          <Route path="/campaigns/:id" element={<p>Campaign created</p>} />
        </Routes>
      </MemoryRouter>,
    ),
  );
}

it("shows separate active campaigns for the same adventure and keeps archived-adventure campaigns visible", async () => {
  installFakeApi([
    [/^GET \/api\/users\/me$/, () => json({ id: 1 })],
    [
      /^GET \/api\/campaigns$/,
      () =>
        json([
          campaign,
          { ...campaign, id: 22, title: "Saturday party", characters: [] },
          { ...campaign, id: 23, status: "ended", title: "Old party" },
        ]),
    ],
    [/^GET \/api\/adventures$/, () => json([adventure])],
    [/^GET \/api\/sessions$/, () => json([])],
  ]);
  await render("/campaigns");
  expect(container.querySelectorAll("article")).toHaveLength(2);
  expect(container.textContent).toContain("Friday party");
  expect(container.textContent).toContain("Saturday party");
  expect(container.textContent).toContain("Rowan");
  expect(container.textContent).not.toContain("Old party");
  const ended = [...container.querySelectorAll("button")].find((button) =>
    button.textContent?.includes("Ended ("),
  )!;
  await act(() => ended.click());
  expect(container.querySelectorAll("article")).toHaveLength(1);
  expect(container.textContent).toContain("Old party");
});

it("creates a named campaign with multiple characters and recovers the exact request after reload", async () => {
  let attempts = 0;
  const { calls } = installFakeApi([
    [/^GET \/api\/adventures\/sheep$/, () => json(adventure)],
    [
      /^GET \/api\/characters$/,
      () =>
        json(
          [11, 12].map((id) => ({
            id,
            name: id === 11 ? "Rowan" : "Briar",
            character_class: "Wizard",
            level: 1,
            current_assignment: null,
          })),
        ),
    ],
    [
      /^POST \/api\/adventures\/sheep\/campaign$/,
      () => (++attempts === 1 ? json({}, 503) : json(campaign)),
    ],
  ]);
  await render("/adventures/sheep");
  const checkboxes = container.querySelectorAll<HTMLInputElement>(
    'input[type="checkbox"]',
  );
  await act(() => {
    checkboxes[0].click();
    checkboxes[1].click();
  });
  await act(() =>
    container
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  expect(container.textContent).toContain("Your creation request is saved");
  await render("/adventures/sheep", "reload");
  expect(
    container.querySelector<HTMLInputElement>('input[type="checkbox"]')!
      .checked,
  ).toBe(true);
  expect(
    container
      .querySelector<HTMLInputElement>('input[type="checkbox"]')!
      .matches(":disabled"),
  ).toBe(true);
  await act(() =>
    container
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  const posts = calls.filter((call) => call.method === "POST");
  expect(posts).toHaveLength(2);
  expect(posts[0].body).toEqual(posts[1].body);
  expect(posts[0].body).toMatchObject({
    title: adventure.title,
    character_ids: [11, 12],
    new_campaign: true,
  });
  expect(container.textContent).toContain("Campaign created");
  expect(sessionStorage.getItem("edgar:new-campaign:sheep")).toBeNull();
});

it("requires a character and never moves a character out of an existing campaign", async () => {
  const { calls } = installFakeApi([
    [/^GET \/api\/adventures\/sheep$/, () => json(adventure)],
    [
      /^GET \/api\/characters$/,
      () =>
        json([
          {
            id: 11,
            name: "Rowan",
            character_class: "Wizard",
            level: 1,
            current_assignment: {
              campaign_id: 21,
              campaign_title: "Friday party",
            },
          },
        ]),
    ],
  ]);
  await render("/adventures/sheep");
  expect(
    container
      .querySelector<HTMLInputElement>('input[type="checkbox"]')!
      .matches(":disabled"),
  ).toBe(true);
  expect(
    container.querySelector<HTMLButtonElement>('button[type="submit"]')!
      .disabled,
  ).toBe(true);
  await act(() =>
    container
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(0);
});
