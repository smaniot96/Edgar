import { describe, expect, it } from "vitest";

import { resolveAdventureCoverUrl } from "./adventureCover";

describe("adventure cover URL contract", () => {
  it.each([
    ["assets/bridge.svg", "/ui/assets/bridge.svg"],
    ["assets/covers/bridge.svg", "/ui/assets/covers/bridge.svg"],
    ["ui/assets/bridge.svg", "/ui/assets/bridge.svg"],
    ["/ui/assets/bridge.svg", "/ui/assets/bridge.svg"],
    ["api/sessions/42/assets/map.svg", "/api/sessions/42/assets/map.svg"],
    ["/api/sessions/42/assets/map.svg", "/api/sessions/42/assets/map.svg"],
  ])("resolves %s to %s", (stored, expected) => {
    expect(resolveAdventureCoverUrl(stored, "/ui/")).toBe(expected);
  });

  it.each([
    null,
    undefined,
    "",
    "https://example.test/cover.jpg",
    "//example.test/cover.jpg",
    "javascript:alert(1)",
    "data:image/svg+xml,<svg/>",
    "assets/../private/cover.svg",
    "assets/%2e%2e/private.svg",
    "assets//cover.svg",
    "assets/cover.svg?token=1",
    "assets/cover.svg#fragment",
    "assets\\cover.svg",
    "private/cover.svg",
  ])("rejects unsafe or absent value %s", (stored) => {
    expect(resolveAdventureCoverUrl(stored)).toBeNull();
  });
});
