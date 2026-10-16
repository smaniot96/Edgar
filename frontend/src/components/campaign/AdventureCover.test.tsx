// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { AdventureCover } from "./AdventureCover";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubEnv("BASE_URL", "/ui/");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

it("shows the useful initial fallback when the cover is absent or unsupported", async () => {
  await act(() => root.render(<AdventureCover coverImage={null} name="The Sunken Keep" />));
  expect(container.textContent).toBe("S");
  expect(container.querySelector("img")).toBeNull();

  await act(() => root.render(<AdventureCover coverImage="javascript:alert(1)" name="Bridge" />));
  expect(container.textContent).toBe("B");
  expect(container.querySelector("img")).toBeNull();
});

it("falls back when an accepted cover URL fails to load", async () => {
  await act(() => root.render(<AdventureCover coverImage="assets/edg129-fixture.svg" name="The Bridge" />));
  const image = container.querySelector("img")!;
  expect(image.getAttribute("src")).toBe("/ui/assets/edg129-fixture.svg");

  await act(() => image.dispatchEvent(new Event("error")));
  expect(container.querySelector("img")).toBeNull();
  expect(container.textContent).toBe("B");
});
