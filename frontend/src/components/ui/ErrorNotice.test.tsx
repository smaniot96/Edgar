// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { ErrorNotice } from "./ErrorNotice";

const PRIVATE_SENTINEL = "private prompt and sk-secret-api-key";

let cleanup: (() => void) | undefined;
afterEach(() => { cleanup?.(); cleanup = undefined; });

it("renders fixed category guidance and hides raw response details", async () => {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  cleanup = () => { root.unmount(); container.remove(); };

  await act(() => root.render(<ErrorNotice error={{
    code: "quota_exceeded",
    message: PRIVATE_SENTINEL,
    detail: PRIVATE_SENTINEL,
    support_ref: "err-invalid-reference",
  }} />));

  expect(container.textContent).toContain("configured service allowance has been reached");
  expect(container.textContent).not.toContain("Your current form entries are still here.");
  expect(container.textContent).not.toContain(PRIVATE_SENTINEL);
  expect(container.textContent).not.toContain("err-invalid-reference");
});

it("offers a read-only check action for uncertain saves", async () => {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  cleanup = () => { root.unmount(); container.remove(); };
  const checkAgain = vi.fn();

  await act(() => root.render(<ErrorNotice
    error={{ code: "turn_save_failed", outcome: "unknown" }}
    onRetry={checkAgain}
  />));

  const button = Array.from(container.querySelectorAll("button")).find((item) => item.textContent === "Check again");
  expect(button).toBeDefined();
  await act(() => button?.click());
  expect(checkAgain).toHaveBeenCalledOnce();
  expect(container.textContent).not.toContain("Your current form entries are still here.");
});
