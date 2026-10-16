import { expect, it } from "vitest";
import { safeFailureFromUnknown, safeTurnError } from "./turnStream";

const PRIVATE_SENTINEL = "private player plan and provider token sk-secret-123";

it("projects provider failures from fixed category copy and validates support references", () => {
  const failure = safeTurnError({
    code: "provider_unavailable",
    category: "provider_unavailable",
    message: PRIVATE_SENTINEL,
    detail: PRIVATE_SENTINEL,
    recovery_action: "check_existing_result",
    outcome: "unknown",
    support_ref: "err-0123456789abcdef",
  });

  expect(failure).toMatchObject({
    category: "provider_unavailable",
    recoveryAction: "check_existing_result",
    supportRef: "err-0123456789abcdef",
  });
  expect(JSON.stringify(failure)).not.toContain(PRIVATE_SENTINEL);
  expect(safeTurnError({ code: "provider_unavailable", support_ref: PRIVATE_SENTINEL }).supportRef).toBeUndefined();
});

it("uses offline-safe copy for a network error without rendering its message", () => {
  const failure = safeFailureFromUnknown(new TypeError(PRIVATE_SENTINEL));

  expect(failure.category).toBe("interrupted");
  expect(failure.outcome).toBe("unknown");
  expect(failure.message).not.toContain(PRIVATE_SENTINEL);
});

it("maps readiness codes even when the server detail contains private text", () => {
  const failure = safeTurnError({
    detail: {
      code: "rules_edition_mismatch",
      message: PRIVATE_SENTINEL,
      support_ref: "err-bad-ref",
    },
  });

  expect(failure).toMatchObject({ category: "validation", recoveryAction: "fix_input" });
  expect(failure.message).toContain("rules edition");
  expect(failure.supportRef).toBeUndefined();
  expect(JSON.stringify(failure)).not.toContain(PRIVATE_SENTINEL);
});

it("preserves the distinct safe code for a turn save failure", () => {
  const failure = safeTurnError({ code: "turn_save_failed", support_ref: "err-0123456789abcdef" });

  expect(failure).toMatchObject({
    code: "turn_save_failed",
    category: "internal",
    recoveryAction: "check_existing_result",
    supportRef: "err-0123456789abcdef",
  });
});
