// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { useVisualViewportStyle } from "./useVisualViewportStyle";

type FakeViewport = EventTarget & { height: number; offsetTop: number; scale: number };

function viewportFixture() {
  const viewport = Object.assign(new EventTarget(), { height: 844, offsetTop: 0, scale: 1 }) as FakeViewport;
  Object.defineProperty(window, "visualViewport", { configurable: true, value: viewport });
  return viewport;
}

function Harness() {
  const style = useVisualViewportStyle();
  return <div data-testid="shell" style={style} />;
}

let container: HTMLDivElement | null = null;
let unmount: (() => void) | null = null;

afterEach(() => {
  unmount?.();
  container?.remove();
  container = null;
  unmount = null;
  Object.defineProperty(window, "visualViewport", { configurable: true, value: undefined });
  vi.unstubAllGlobals();
});

function mount() {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  unmount = () => { void act(() => root.unmount()); };
  act(() => root.render(<Harness />));
  return container.querySelector<HTMLElement>('[data-testid="shell"]')!;
}

it("tracks visible height and offset on keyboard viewport resize, then restores CSS viewport fallback on pinch zoom", () => {
  const viewport = viewportFixture();
  const shell = mount();
  expect(shell.style.getPropertyValue("--edgar-visible-height")).toBe("844px");
  expect(shell.style.getPropertyValue("--edgar-visible-top")).toBe("0px");

  act(() => {
    viewport.height = 420;
    viewport.offsetTop = 24;
    viewport.dispatchEvent(new Event("resize"));
    viewport.dispatchEvent(new Event("scroll"));
  });
  expect(shell.style.getPropertyValue("--edgar-visible-height")).toBe("420px");
  expect(shell.style.getPropertyValue("--edgar-visible-top")).toBe("24px");

  act(() => {
    viewport.scale = 2;
    viewport.dispatchEvent(new Event("resize"));
  });
  expect(shell.style.getPropertyValue("--edgar-visible-height")).toBe("");
  expect(shell.style.getPropertyValue("--edgar-visible-top")).toBe("");
});

it("removes visual viewport listeners when the app shell unmounts", () => {
  const viewport = viewportFixture();
  const add = vi.spyOn(viewport, "addEventListener");
  const remove = vi.spyOn(viewport, "removeEventListener");
  mount();
  expect(add.mock.calls.map(([event]) => event)).toEqual(["resize", "scroll"]);
  unmount?.();
  unmount = null;
  expect(remove.mock.calls.map(([event]) => event)).toEqual(["resize", "scroll"]);
});
