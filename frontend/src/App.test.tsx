// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, it, vi } from "vitest";
import App from "./App";
vi.mock("./dev/ModeContext", () => ({ useMode: () => ({ isDev: false, toggleMode: vi.fn() }) }));

it("moves focus to the new page heading after a route change, but not on first load or same-page updates", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(() => root.render(
      <MemoryRouter initialEntries={["/campaigns"]}>
        <Routes>
          <Route element={<App />}>
            <Route path="campaigns" element={<main><h1>Campaigns</h1><Link to="/settings">Go to settings</Link><Link to="/campaigns?filter=ended">Filter ended</Link></main>} />
            <Route path="settings" element={<main><h1>Settings</h1></main>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    ));
    const link = (name: string) => Array.from(container.querySelectorAll("a")).find((item) => item.textContent === name)!;
    expect(document.activeElement).toBe(document.body);

    link("Filter ended").focus();
    await act(() => link("Filter ended").click());
    expect(document.activeElement).toBe(link("Filter ended"));

    link("Go to settings").focus();
    await act(() => link("Go to settings").click());
    expect(document.activeElement?.tagName).toBe("H1");
    expect(document.activeElement?.textContent).toBe("Settings");
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});

it("falls back to the page region when the new route has no heading", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(() => root.render(
      <MemoryRouter initialEntries={["/campaigns"]}>
        <Routes>
          <Route element={<App />}>
            <Route path="campaigns" element={<main><Link to="/play/1">Open play</Link></main>} />
            <Route path="play/:id" element={<main>Saved adventure</main>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    ));
    await act(() => container.querySelector<HTMLAnchorElement>("a[href='/play/1']")!.click());
    expect(document.activeElement?.contains(container.querySelector("main"))).toBe(true);
    expect(document.activeElement?.getAttribute("tabindex")).toBe("-1");
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});

it("applies the visible keyboard viewport to the shell and restores the zoom fallback", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const viewport = Object.assign(new EventTarget(), { height: 844, offsetTop: 0, scale: 1 });
  Object.defineProperty(window, "visualViewport", { configurable: true, value: viewport });
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(() => root.render(<MemoryRouter><App /></MemoryRouter>));
    const shell = container.firstElementChild as HTMLElement;
    expect(shell.style.getPropertyValue("--edgar-visible-height")).toBe("844px");
    await act(() => {
      viewport.height = 340; viewport.offsetTop = 20;
      viewport.dispatchEvent(new Event("resize"));
    });
    expect(shell.style.getPropertyValue("--edgar-visible-height")).toBe("340px");
    expect(shell.style.getPropertyValue("--edgar-visible-top")).toBe("20px");
    await act(() => { viewport.scale = 2; viewport.dispatchEvent(new Event("resize")); });
    expect(shell.style.getPropertyValue("--edgar-visible-height")).toBe("");
  } finally {
    await act(() => root.unmount()); container.remove();
    Object.defineProperty(window, "visualViewport", { configurable: true, value: undefined });
    vi.unstubAllGlobals();
  }
});
