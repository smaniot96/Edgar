/** Browser observations complement the existing server turn diagnostics. No story/input is retained. */
export type BrowserTurnStage = "busy_paint" | "headers" | "first_token" | "first_text_paint" | "done" | "composer_ready" | "journal_settled" | "map_settled" | "failed";
export interface BrowserTurnTimeline {
  sessionId: number;
  requestId: string;
  attempt: number;
  startedAt: number;
  /** Milliseconds since submit. Null/unset is unmeasured, never inferred from headers. */
  stages: Partial<Record<BrowserTurnStage, number>>;
}
const records: BrowserTurnTimeline[] = [];
const subscribers = new Set<() => void>();
const notify = () => subscribers.forEach((listener) => listener());
const prefix = (row: BrowserTurnTimeline) => `edgar:turn:${row.requestId}:${row.attempt}`;

export function beginBrowserTurn(sessionId: number, requestId: string) {
  const attempt = (records.findLast((row) => row.requestId === requestId)?.attempt ?? 0) + 1;
  const row: BrowserTurnTimeline = { sessionId, requestId, attempt, startedAt: performance.now(), stages: {} };
  records.push(row);
  if (records.length > 100) {
    const old = records.shift()!;
    for (const stage of ["submit", ...Object.keys(old.stages)]) {
      performance.clearMarks?.(`${prefix(old)}:${stage}`);
      performance.clearMeasures?.(`${prefix(old)}:${stage}`);
    }
  }
  performance.mark?.(`${prefix(row)}:submit`);
  notify();
}

export function markBrowserTurn(requestId: string, stage: BrowserTurnStage) {
  const row = records.findLast((record) => record.requestId === requestId);
  if (!row || row.stages[stage] !== undefined) return;
  row.stages[stage] = Math.max(0, performance.now() - row.startedAt);
  const name = `${prefix(row)}:${stage}`;
  performance.mark?.(name);
  try { performance.measure?.(name, `${prefix(row)}:submit`, name); } catch { /* older browsers */ }
  notify();
}

/** Two animation frames place the observation after the DOM commit's next paint opportunity.
 * Hidden tabs cannot provide a visible-paint observation, so leave it unmeasured there. */
export function afterBrowserPaint(callback: () => void): () => void {
  if (typeof requestAnimationFrame !== "function" || document.visibilityState === "hidden") return () => {};
  let second = 0;
  const first = requestAnimationFrame(() => { second = requestAnimationFrame(() => {
    if (document.visibilityState !== "hidden") callback();
  }); });
  return () => { cancelAnimationFrame(first); if (second) cancelAnimationFrame(second); };
}

export function browserTurnTimelines(sessionId?: number): BrowserTurnTimeline[] {
  return records.filter((row) => sessionId === undefined || row.sessionId === sessionId).map((row) => ({ ...row, stages: { ...row.stages } }));
}
export function subscribeBrowserTurnTimelines(listener: () => void) {
  subscribers.add(listener);
  return () => { subscribers.delete(listener); };
}
