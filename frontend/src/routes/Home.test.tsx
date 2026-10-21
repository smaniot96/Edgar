// @vitest-environment jsdom
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import type { AdventureRead } from "../api/types";
import Home from "./Home";
const data = vi.hoisted(() => ({
  adventures: [] as AdventureRead[],
  error: null as unknown,
}));
vi.mock("../lib/useQuery", () => ({
  useQuery: (key: string) => ({
    data:
      key === "me"
        ? { id: 1, email: "player@example.test" }
        : data.error
          ? undefined
          : data.adventures,
    error: key === "adventures" ? data.error : null,
    loading: false,
    refetch: vi.fn(),
    setData: vi.fn(),
  }),
}));
beforeEach(() => {
  data.adventures = [];
  data.error = null;
});
it("shows reusable adventures as detail links, with no campaigns or resume actions", () => {
  data.adventures = [
    {
      slug: "sheep",
      title: "The Wild Sheep Chase",
      description: "A talking sheep needs help.",
      level_range: "4–5",
      cover_image: null,
      status: "ready",
    },
  ];
  const html = renderToStaticMarkup(
    <MemoryRouter>
      <Home />
    </MemoryRouter>,
  );
  expect(html).toContain('href="/adventures/sheep"');
  expect(html).toContain("A talking sheep needs help.");
  expect(html).not.toContain("Campaign #");
  expect(html).not.toContain("Resume");
});
it("keeps archived duplicates out of the default adventure catalogue", () => {
  data.adventures = [
    {
      slug: "old",
      title: "Old duplicate",
      description: null,
      level_range: null,
      cover_image: null,
      library_archived: true,
    },
  ];
  const html = renderToStaticMarkup(
    <MemoryRouter>
      <Home />
    </MemoryRouter>,
  );
  expect(html).not.toContain("Old duplicate");
  expect(html).toContain("Show archived (1)");
});
it("shows a loading failure without claiming the library is empty", () => {
  data.error = new Error("Store down");
  const html = renderToStaticMarkup(
    <MemoryRouter>
      <Home />
    </MemoryRouter>,
  );
  expect(html).not.toContain("No adventures yet");
  expect(html).not.toContain("Store down");
});
