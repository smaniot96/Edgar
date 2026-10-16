import { describe, expect, it } from "vitest";

import type { ProviderCallDiagnostics, TurnDiagnostics } from "../api/types";
import {
  UNKNOWN,
  callSummary,
  fmtCostMicroUsd,
  fmtCount,
  fmtMs,
  stageRows,
} from "./turnDiagnostics";

const unknownCall: ProviderCallDiagnostics = {
  kind: "parse",
  profile: "play.parser",
  requested_model: null,
  resolved_model: null,
  streamed: false,
  latency_ms: null,
  first_text_ms: null,
  attempts: null,
  error_category: null,
  usage: { input_tokens: null, output_tokens: null, total_tokens: null },
  cost_microusd: null,
};

describe("unknown is never rendered as zero", () => {
  it("formats unmeasured numbers as unknown and measured zero as zero", () => {
    expect(fmtMs(null)).toBe(UNKNOWN);
    expect(fmtMs(undefined)).toBe(UNKNOWN);
    expect(fmtMs(0)).toBe("0 ms");
    expect(fmtCount(null)).toBe(UNKNOWN);
    expect(fmtCount(0)).toBe("0");
    expect(fmtCostMicroUsd(null)).toBe(UNKNOWN);
    expect(fmtCostMicroUsd(0)).toBe("$0.000000");
    expect(fmtCostMicroUsd(1_500_000)).toBe("$1.500000");
  });

  it("lists every shared stage in order, with unmeasured stages marked unknown", () => {
    const diag = {
      stages_ms: { parsing_ms: 0, narrating_ms: 33 },
    } as unknown as TurnDiagnostics;
    expect(stageRows(diag)).toEqual([
      ["lock_wait_ms", UNKNOWN],
      ["parsing_ms", "0 ms"],
      ["retrieving_ms", UNKNOWN],
      ["adjudicating_ms", UNKNOWN],
      ["narrating_ms", "33 ms"],
      ["saving_ms", UNKNOWN],
    ]);
  });

  it("summarises a call without inventing usage, cost or model", () => {
    const text = callSummary(unknownCall);
    expect(text).toContain("in unknown / out unknown");
    expect(text).toContain("cost unknown");
    expect(text).toContain("latency unknown");
    expect(text).not.toMatch(/\b0 ms\b|\$0/);
  });
});
