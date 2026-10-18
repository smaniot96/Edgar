// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { setApiToken } from "../../api/auth";
import { AuthorizedImage } from "./AuthorizedImage";
import { ApiDownloadLink } from "./ApiDownloadLink";

// Fake test token only; it exists only in this file.
const TEST_TOKEN = "edgar_resources_fixture_token_0123";
const URL_ = "/api/sessions/12/assets/handout-1";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", vi.fn(async () => new Response(new Blob(["bytes"]), { status: 200 })));
  vi.stubGlobal("URL", Object.assign(URL, {
    createObjectURL: vi.fn(() => "blob:edgar-fixture"),
    revokeObjectURL: vi.fn(),
  }));
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

it("local mode: the image uses its plain URL and nothing is fetched", async () => {
  await act(() => root.render(<AuthorizedImage url={URL_} alt="Artwork" loading="lazy" />));
  const img = container.querySelector("img")!;
  expect(img.getAttribute("src")).toBe(URL_);
  expect(img.getAttribute("loading")).toBe("lazy");
  expect(fetch).not.toHaveBeenCalled();
});

it("local mode: the download is a plain link with no fetch", async () => {
  await act(() => root.render(<ApiDownloadLink url={URL_} filename="handout.pdf">Open player handout</ApiDownloadLink>));
  const link = container.querySelector("a")!;
  expect(link.getAttribute("href")).toBe(URL_);
  expect(link.hasAttribute("download")).toBe(false);
  expect(fetch).not.toHaveBeenCalled();
});

it("token mode: the image is fetched with the Authorization header and shown from an object URL", async () => {
  setApiToken(TEST_TOKEN);
  await act(() => root.render(<AuthorizedImage url={URL_} alt="Artwork" />));
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  expect(fetch).toHaveBeenCalledTimes(1);
  const [calledUrl, init] = vi.mocked(fetch).mock.calls[0] as unknown as [string, RequestInit];
  expect(calledUrl).toBe(URL_);
  expect(new Headers(init.headers).get("authorization")).toBe(`Bearer ${TEST_TOKEN}`);
  expect(container.querySelector("img")?.getAttribute("src")).toBe("blob:edgar-fixture");
  expect(container.innerHTML).not.toContain(TEST_TOKEN);
});

it("token mode: the download is a button that saves through the authorized client", async () => {
  setApiToken(TEST_TOKEN);
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  await act(() => root.render(<ApiDownloadLink url={URL_} filename="handout.pdf">Open player handout</ApiDownloadLink>));
  expect(container.querySelector("a")).toBeNull();
  const button = container.querySelector("button")!;
  await act(async () => { button.click(); await Promise.resolve(); await Promise.resolve(); });
  expect(fetch).toHaveBeenCalledTimes(1);
  const [, init] = vi.mocked(fetch).mock.calls[0] as unknown as [string, RequestInit];
  expect(new Headers(init.headers).get("authorization")).toBe(`Bearer ${TEST_TOKEN}`);
  expect(click).toHaveBeenCalledTimes(1);
  click.mockRestore();
});
