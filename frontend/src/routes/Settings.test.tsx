// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { getMe } from "../api/client";
import Settings from "./Settings";

// Fake test token only. It exists only in this file and is never a real credential.
const TEST_TOKEN = "edgar_settings_fixture_token_abcdef";
const TOKEN_KEY = "edgar.apiToken";

vi.mock("../api/client", async (original) => ({ ...await original<object>(), getMe: vi.fn(), updateMe: vi.fn() }));

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(getMe).mockResolvedValue({ id: 1, email: "player@example.test", display_name: null } as never);
  sessionStorage.clear();
  localStorage.clear();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  sessionStorage.clear();
  localStorage.clear();
  vi.unstubAllGlobals();
});

function setInputValue(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  setter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

it("saves an access token to this tab only, then clears the field and never shows it", async () => {
  await act(() => root.render(<Settings />));
  const tokenForm = container.querySelectorAll("form")[0]!;
  const input = container.querySelector<HTMLInputElement>("input[type='password']")!;
  expect(input).not.toBeNull();

  await act(async () => setInputValue(input, TEST_TOKEN));
  await act(async () => { tokenForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });

  expect(sessionStorage.getItem(TOKEN_KEY)).toBe(TEST_TOKEN);
  expect(localStorage.length).toBe(0);
  expect(input.value).toBe("");
  expect(container.textContent).toContain("A token is set for this tab.");
  expect(container.textContent).not.toContain(TEST_TOKEN);
  expect(container.innerHTML).not.toContain(TEST_TOKEN);
});

it("removes the stored token on request", async () => {
  sessionStorage.setItem(TOKEN_KEY, TEST_TOKEN);
  await act(() => root.render(<Settings />));
  const remove = [...container.querySelectorAll("button")].find((b) => b.textContent === "Remove token")!;
  await act(async () => { remove.click(); });
  expect(sessionStorage.getItem(TOKEN_KEY)).toBeNull();
  expect(container.textContent).toContain("No token is set for this tab.");
});

it("refuses a malformed token with an explicit message and stores nothing", async () => {
  await act(() => root.render(<Settings />));
  const tokenForm = container.querySelectorAll("form")[0]!;
  const input = container.querySelector<HTMLInputElement>("input[type='password']")!;
  await act(async () => setInputValue(input, "has a space"));
  await act(async () => { tokenForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
  expect(sessionStorage.getItem(TOKEN_KEY)).toBeNull();
  expect(container.querySelector("[role='alert']")?.textContent).toContain("not an access token");
});
