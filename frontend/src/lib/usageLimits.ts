/** Copy for the daily usage allowance (EDG-066): the reason, the reset time and the budget gap. */

export interface DailyBudgetGap {
  requestedMicrousd: number;
  remainingMicrousd: number;
}

/** "USD 49.50" from USD micro-units. */
export function formatUsd(microusd: number): string {
  return `USD ${(microusd / 1_000_000).toFixed(2)}`;
}

/** "about 16 hours", "about 5 minutes", "less than a minute". */
export function formatWait(seconds: number): string {
  if (seconds < 60) return "less than a minute";
  if (seconds < 3600) {
    const minutes = Math.ceil(seconds / 60);
    return `about ${minutes} minute${minutes === 1 ? "" : "s"}`;
  }
  const hours = Math.round(seconds / 3600);
  return `about ${hours} hour${hours === 1 ? "" : "s"}`;
}

/** True when the request itself is larger than what is left today. */
export function exceedsRemaining(gap?: DailyBudgetGap): gap is DailyBudgetGap {
  return !!gap && gap.requestedMicrousd > gap.remainingMicrousd;
}

export function dailyBudgetMessage(gap?: DailyBudgetGap): string {
  if (exceedsRemaining(gap) && gap.remainingMicrousd > 0) {
    return `This preparation asks for ${formatUsd(gap.requestedMicrousd)}, but only ${formatUsd(gap.remainingMicrousd)} of today's usage allowance is left, so it was not started.`;
  }
  return "Today's usage allowance has been used up, so this was not started.";
}

export function dailyBudgetNextStep(gap?: DailyBudgetGap, retryAfterSeconds?: number): string {
  const reset = retryAfterSeconds
    ? `the allowance resets at midnight UTC, in ${formatWait(retryAfterSeconds)}`
    : "the allowance resets at midnight UTC";
  if (exceedsRemaining(gap) && gap.remainingMicrousd > 0) {
    return `Lower the preparation budget to ${formatUsd(gap.remainingMicrousd)} or less, or try again after ${reset}.`;
  }
  return `Try again after ${reset}.`;
}
