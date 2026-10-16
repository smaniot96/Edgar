import type { ProviderCallDiagnostics, TurnDiagnostics } from "../api/types";

/** Shown for every value the turn did not measure. Never rendered as 0 (EDG-053). */
export const UNKNOWN = "unknown";

const STAGE_ORDER = [
  "lock_wait_ms",
  "parsing_ms",
  "retrieving_ms",
  "adjudicating_ms",
  "narrating_ms",
  "saving_ms",
] as const;

export function fmtMs(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value} ms` : UNKNOWN;
}

export function fmtCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : UNKNOWN;
}

/** Cost is stored in micro-USD; shown in USD with enough digits for sub-cent calls. */
export function fmtCostMicroUsd(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return UNKNOWN;
  return `$${(value / 1_000_000).toFixed(6)}`;
}

export function fmtText(value: string | null | undefined): string {
  return value ? value : UNKNOWN;
}

/** Every shared stage, in pipeline order, with its measured value or "unknown". */
export function stageRows(diag: TurnDiagnostics): [string, string][] {
  return STAGE_ORDER.map((stage) => [stage, fmtMs(diag.stages_ms[stage])]);
}

export function callSummary(call: ProviderCallDiagnostics): string {
  const tokens = call.usage;
  return [
    `${call.kind}${call.streamed ? " (stream)" : ""}`,
    fmtText(call.resolved_model ?? call.requested_model),
    `latency ${fmtMs(call.latency_ms)}`,
    `first text ${fmtMs(call.first_text_ms)}`,
    `in ${fmtCount(tokens.input_tokens)} / out ${fmtCount(tokens.output_tokens)}`,
    `cost ${fmtCostMicroUsd(call.cost_microusd)}`,
    call.error_category ? `error ${call.error_category}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}
