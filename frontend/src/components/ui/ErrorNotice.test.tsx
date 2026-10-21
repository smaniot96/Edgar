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

it("keeps read-only refresh available for internal and source list failures", async () => {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  cleanup = () => { root.unmount(); container.remove(); };
  const refresh = vi.fn();
  for (const code of ["turn_failed", "campaign_sources_unavailable"]) {
    await act(() => root.render(<ErrorNotice error={{ code, outcome: "not_committed" }} onRetry={refresh} />));
    const button = container.querySelector("button");
    expect(button?.textContent).toBe("Check again");
    await act(() => button?.click());
  }
  expect(refresh).toHaveBeenCalledTimes(2);
});

it("names the daily allowance, its reset time and the budget gap instead of a rate limit (EDG-055 F7)", async () => {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  cleanup = () => { root.unmount(); container.remove(); };
  const { ApiError } = await import("../../api/client");
  const error = new ApiError(429, JSON.stringify({
    code: "daily_budget_exhausted", category: "quota_exceeded", recovery_action: "wait",
    retryable: false, outcome: "not_committed", support_ref: "err-0123456789abcdef",
    retry_after_seconds: 57_600,
    daily_budget: { requested_microusd: 50_000_000, remaining_microusd: 49_500_000, daily_limit_microusd: 50_000_000 },
  }));

  await act(() => root.render(<ErrorNotice error={error} />));

  const text = container.textContent ?? "";
  expect(text).not.toContain("too many requests");
  expect(text).not.toContain("Check save status");
  expect(text).toContain("This preparation asks for USD 50.00, but only USD 49.50 of today's usage allowance is left");
  expect(text).toContain("Lower the preparation budget to USD 49.50 or less");
  expect(text).toContain("resets at midnight UTC, in about 16 hours");
});

it("says the daily allowance is used up when nothing is left", async () => {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  cleanup = () => { root.unmount(); container.remove(); };

  await act(() => root.render(<ErrorNotice error={{
    code: "daily_budget_exhausted", retry_after_seconds: 1_800,
    daily_budget: { requested_microusd: 500_000, remaining_microusd: 0 },
  }} />));

  const text = container.textContent ?? "";
  expect(text).toContain("Today's usage allowance has been used up, so this was not started.");
  expect(text).toContain("Try again after the allowance resets at midnight UTC, in about 30 minutes.");
  expect(text).not.toContain("Lower the preparation budget");
});
