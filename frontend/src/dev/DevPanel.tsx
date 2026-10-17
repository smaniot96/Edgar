import { useState } from "react";
import type { ReactNode } from "react";

import type {
  AdjudicationResult,
  CombatRoundDiagnostics,
  CombatState,
  DebugPayload,
  RetrievedChunk,
  TurnDiagnostics,
  TurnTraceRag,
  TurnTraceRecord,
} from "../api/types";
import type { TraceState } from "../components/play/turnState";
import { cn } from "../lib/cn";
import {
  UNKNOWN,
  callSummary,
  fmtCostMicroUsd,
  fmtCount,
  fmtMs,
  fmtText,
  stageRows,
} from "./turnDiagnostics";

function Section({
  title,
  defaultOpen = true,
  warn = false,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  warn?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-t border-line first:border-t-0">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={cn(
          "flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs font-semibold uppercase tracking-wide",
          warn ? "text-ember-300" : "text-ink-muted",
        )}
      >
        <span className="text-ink-subtle">{open ? "▾" : "▸"}</span>
        <span>{title}</span>
      </button>
      {open ? <div className="px-3 pb-3 pt-0">{children}</div> : null}
    </div>
  );
}

function KV({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex gap-2 py-0.5">
      <span className="min-w-[140px] shrink-0 text-ink-subtle">{k}</span>
      <span className="break-words font-mono text-ink">{v}</span>
    </div>
  );
}

/** A stage the turn did not measure is labelled, never shown as zero (EDG-031). */
function timing(v: number | undefined): ReactNode {
  if (v === undefined) return <span className="text-ink-subtle">unavailable</span>;
  return mono(v);
}

function mono(v: unknown): ReactNode {
  if (v === null || v === undefined) return <span className="text-ink-subtle">—</span>;
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function list(arr: unknown): ReactNode {
  if (!Array.isArray(arr) || arr.length === 0) return <span className="text-ink-subtle">—</span>;
  return arr.map((x) => String(x)).join(", ");
}

function ChunkList({ chunks, label }: { chunks: RetrievedChunk[]; label: string }) {
  const empty = !chunks || chunks.length === 0;
  return (
    <div className="mt-1">
      <div
        className={cn(
          "mb-1 inline-block rounded px-1.5 py-0.5 text-xs font-semibold",
          empty
            ? "bg-red-900/50 text-red-200"
            : "bg-surface text-ink-muted",
        )}
      >
        {label}: {empty ? "0 retrieved ⚠" : `${chunks.length} retrieved`}
      </div>
      {empty ? null : (
        <ol className="flex flex-col gap-1.5">
          {chunks.map((c, i) => (
            <li key={i} className="rounded border border-line bg-surface-sunken p-2 text-xs">
              <div className="mb-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-ink-subtle">
                <span>#{i + 1}</span>
                <span>{c.collection}</span>
                <span>score {typeof c.score === "number" ? c.score.toFixed(3) : "—"}</span>
                <span>{c.source}</span>
                {c.page != null ? <span>p.{c.page}</span> : null}
              </div>
              <div className="whitespace-pre-wrap break-words font-mono text-ink/85">{c.text}</div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function CombatBlock({ combat }: { combat: CombatState }) {
  return (
    <div className="text-xs">
      <div className="mb-1 flex flex-wrap gap-x-3 gap-y-0.5">
        <KV k="round" v={mono(combat.round)} />
        <KV k="ended" v={mono(combat.ended)} />
        <KV k="outcome" v={mono(combat.outcome)} />
        <KV k="second_wind_used" v={mono(combat.second_wind_used)} />
      </div>
      {combat.initiative_order && combat.initiative_order.length > 0 ? (
        <KV k="initiative_order" v={list(combat.initiative_order)} />
      ) : null}
      <div className="mt-2 overflow-x-auto">
        <table className="w-full border-collapse text-left font-mono text-[11px]">
          <thead className="text-ink-subtle">
            <tr>
              {["name", "player", "hp", "ac", "atk", "dmg", "alive"].map((h) => (
                <th key={h} className="border-b border-line px-1.5 py-1 font-semibold">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="text-ink">
            {combat.combatants.map((c, i) => (
              <tr key={i} className={cn(!c.alive && "text-ink-subtle line-through")}>
                <td className="px-1.5 py-1">{c.display_name || c.name}</td>
                <td className="px-1.5 py-1">{c.is_player ? "yes" : "no"}</td>
                <td className="px-1.5 py-1">
                  {c.hp_current}/{c.hp_max}
                </td>
                <td className="px-1.5 py-1">{mono(c.ac)}</td>
                <td className="px-1.5 py-1">{mono(c.attack_bonus)}</td>
                <td className="px-1.5 py-1">{mono(c.damage_dice)}</td>
                <td className="px-1.5 py-1">{c.alive ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Measured latency, usage and cost of one DM turn (EDG-053). Unknown is shown, never zero. */
function TurnDiagnosticsBlock({ diag }: { diag: TurnDiagnostics }) {
  const { totals, retrieval, lock } = diag;
  return (
    <div className="text-xs">
      <div className="mb-1 flex flex-wrap gap-x-3 gap-y-0.5">
        <KV k="path" v={fmtText(diag.path)} />
        <KV k="delivery" v={fmtText(diag.delivery)} />
        <KV k="total" v={fmtMs(diag.total_ms)} />
        <KV k="time to first text" v={fmtMs(diag.time_to_first_text_ms)} />
        <KV k="lock wait" v={lock ? fmtMs(lock.wait_ms) : UNKNOWN} />
      </div>
      {stageRows(diag).map(([stage, value]) => (
        <KV key={stage} k={stage} v={value} />
      ))}
      <KV
        k="retrieval"
        v={
          retrieval
            ? `${fmtMs(retrieval.latency_ms)} · rules ${fmtCount(retrieval.rules_chunks)} · adventure ${fmtCount(retrieval.adventure_chunks)} · degraded ${
                retrieval.degraded ? "yes" : "no"
              }`
            : UNKNOWN
        }
      />
      <div className="mt-1 border-t border-line pt-1">
        <KV k="tokens in" v={fmtCount(totals.input_tokens)} />
        <KV k="tokens out" v={fmtCount(totals.output_tokens)} />
        <KV k="tokens total" v={fmtCount(totals.total_tokens)} />
        <KV k="cost (estimate)" v={fmtCostMicroUsd(totals.cost_microusd)} />
        {totals.complete ? null : (
          <div className="mt-0.5 text-ember-300">
            partial total: {totals.calls_unknown_usage} call(s) without usage,{" "}
            {totals.calls_unknown_cost} without cost
          </div>
        )}
        {totals.calls_dropped > 0 ? (
          <div className="mt-0.5 text-ember-300">{totals.calls_dropped} call(s) not recorded (bound)</div>
        ) : null}
      </div>
      {diag.provider_calls.length === 0 ? (
        <div className="mt-1 text-ink-subtle">no provider calls</div>
      ) : (
        <ol className="mt-1 flex flex-col gap-1">
          {diag.provider_calls.map((call, i) => (
            <li key={i} className="break-words font-mono text-ink/85">
              #{i + 1} {callSummary(call)}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** Why a reloaded or live DM turn has no stored trace. Honest per state, never a guess (EDG-138). */
function traceReason(trace: TraceState | null | undefined): string {
  switch (trace?.status) {
    case "loading":
      return "Loading the stored developer trace for this turn…";
    case "not_captured":
      return "No developer trace was stored for this turn: it was sent in User mode, or it predates trace capture (legacy).";
    case "older_than_window":
      return "This turn is older than the loaded trace window, so its trace was not fetched.";
    case "disabled":
      return "Developer debug is off on this server, so no trace is stored or available.";
    case "failed":
      return "The stored developer trace could not be loaded. Reload the session to retry.";
    default:
      return "No debug captured for this turn (sent in User mode).";
  }
}

/** A stored chunk: text only where the server kept it (player audience); citations always. */
function TraceChunkList({ label, side }: { label: string; side: TurnTraceRag }) {
  const empty = side.count === 0;
  return (
    <div className="mt-1">
      <div
        className={cn(
          "mb-1 inline-block rounded px-1.5 py-0.5 text-xs font-semibold",
          empty ? "bg-red-900/50 text-red-200" : "bg-surface text-ink-muted",
        )}
      >
        {label}: {empty ? "0 retrieved ⚠" : `${side.count} retrieved`}
        {side.chunks_dropped > 0 ? ` · ${side.chunks_dropped} not stored` : ""}
      </div>
      {side.chunks.length === 0 ? null : (
        <ol className="flex flex-col gap-1.5">
          {side.chunks.map((c, i) => (
            <li key={i} className="rounded border border-line bg-surface-sunken p-2 text-xs">
              <div className="mb-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-ink-subtle">
                <span>#{i + 1}</span>
                <span>{c.collection ?? "—"}</span>
                <span>score {typeof c.score === "number" ? c.score.toFixed(3) : "—"}</span>
                <span>{c.source ?? "—"}</span>
                {c.page != null ? <span>p.{c.page}</span> : null}
                <span>edition {c.citation.rules_edition ?? "—"}</span>
                <span>{c.citation.edition_status ?? "—"}</span>
                {c.citation.source_sha256 ? (
                  <span>source {c.citation.source_sha256.slice(0, 12)}</span>
                ) : null}
              </div>
              {c.text_withheld ? (
                <div className="italic text-ink-subtle">
                  Text not stored: planner-only or non-player source.
                </div>
              ) : (
                <div className="whitespace-pre-wrap break-words font-mono text-ink/85">{c.text ?? ""}</div>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function CombatRoundSummary({ round }: { round: CombatRoundDiagnostics }) {
  return (
    <div className="text-xs">
      <div className="mb-1 flex flex-wrap gap-x-3 gap-y-0.5">
        <KV k="round" v={mono(round.round)} />
        <KV k="ended" v={mono(round.ended)} />
        <KV k="outcome" v={mono(round.outcome)} />
        <KV k="dice" v={`${round.dice_log.length} roll(s)`} />
      </div>
      <KV k="actors" v={round.actors.map((a) => a.name).join(", ") || "—"} />
      {round.timings_unavailable.length > 0 ? (
        <KV k="timings unavailable" v={list(round.timings_unavailable)} />
      ) : null}
    </div>
  );
}

/** The sections a stored trace reloads into. Companion rows are shown as stored, never recomputed. */
function StoredTraceSections({ record }: { record: TurnTraceRecord }) {
  const trace = record.trace;
  if (!trace) return null;
  const parsed = trace.parsed_input;
  const rag = trace.rag;
  return (
    <>
      <Section title="Intent (stored)">
        {parsed ? (
          <>
            <KV k="intent" v={mono(parsed.intent)} />
            <KV k="check" v={mono(parsed.check)} />
            <KV k="difficulty" v={mono(parsed.difficulty)} />
            <KV k="target" v={mono(parsed.target)} />
            <KV k="retrieval_query" v={mono(parsed.retrieval_query)} />
            <KV k="uses_item" v={mono(parsed.uses_item)} />
            <KV k="lethality" v={mono(parsed.lethality)} />
            <KV k="confirms_ending" v={mono(parsed.confirms_ending)} />
            <KV k="entities" v={mono(parsed.entities)} />
          </>
        ) : (
          <span className="text-ink-subtle">no parsed_input</span>
        )}
      </Section>
      <Section title="RAG retrieval (stored)" warn={rag.rules.count === 0 || rag.adventure.count === 0}>
        <TraceChunkList label="Rules" side={rag.rules} />
        <TraceChunkList label="Adventure" side={rag.adventure} />
      </Section>
      <Section title="Model & settings (stored)" defaultOpen={false}>
        <KV k="narrator profile" v={mono(trace.settings.narrator_profile)} />
        <KV k="rules edition" v={mono(trace.settings.rules_edition)} />
        <KV k="source mode" v={mono(trace.settings.source_mode)} />
        <KV k="profile version" v={mono(trace.settings.profile_version)} />
        {Object.entries(trace.settings.profiles).map(([name, p]) => (
          <KV
            key={name}
            k={name}
            v={`${p.model ?? "—"} · effort ${p.reasoning_effort ?? "—"} · ${p.api ?? "—"}`}
          />
        ))}
      </Section>
      <Section title="Latency, usage & cost (stored)" defaultOpen={false}>
        {record.turn_diagnostics ? (
          <TurnDiagnosticsBlock diag={record.turn_diagnostics} />
        ) : (
          <span className="text-ink-subtle">{UNKNOWN}: no measurement stored for this turn</span>
        )}
      </Section>
      <Section title="Combat round (stored)" defaultOpen={false}>
        {record.combat_round ? (
          <CombatRoundSummary round={record.combat_round} />
        ) : (
          <span className="text-ink-subtle">
            {trace.path === "combat" ? "No combat round row stored for this turn." : "Not a combat turn."}
          </span>
        )}
      </Section>
    </>
  );
}

/**
 * Collapsible developer panel rendered under a DM message in dev mode. Shows
 * everything captured for the turn so correctness can be verified at a glance.
 */
export function DevPanel({
  debug,
  adjudication,
  trace,
}: {
  debug?: DebugPayload | null;
  adjudication?: AdjudicationResult | null;
  /** Stored trace state for a reloaded DM turn (EDG-138); absent for a live turn. */
  trace?: TraceState | null;
}) {
  const stored = trace?.status === "captured" ? trace.record : null;
  if (!debug && !adjudication && !stored) {
    return (
      <div className="mr-auto mt-1 max-w-[820px] rounded-md border border-ember-900/60 bg-surface-sunken px-3 py-2 text-xs italic text-ink-subtle">
        🔧 {traceReason(trace)}
      </div>
    );
  }

  const cu = adjudication?.character_update ?? null;
  const timings = debug?.timings_ms ?? {};
  const storedTrace = stored?.trace ?? null;
  const storedModel = storedTrace
    ? storedTrace.settings.profiles[storedTrace.settings.narrator_profile]?.model
    : undefined;

  return (
    <div className="mr-auto mt-1 w-full max-w-[820px] overflow-hidden rounded-md border border-ember-900/60 bg-canvas text-xs">
      <div className="flex items-center gap-2 border-b border-ember-900/60 bg-ember-950/30 px-3 py-1.5 font-semibold text-ember-200">
        <span>🔧 Dev panel</span>
        {debug?.model || storedModel ? (
          <span className="ml-auto font-mono text-ink-muted">{debug?.model ?? storedModel}</span>
        ) : null}
      </div>

      {!debug && !stored ? (
        <div className="border-b border-line px-3 py-1.5 italic text-ink-subtle">{traceReason(trace)}</div>
      ) : null}
      {!debug && stored ? <StoredTraceSections record={stored} /> : null}

      {debug?.parsed_input !== undefined ? (
        <Section title="Intent">
          {debug.parsed_input ? (
            <>
              <KV k="intent" v={mono(debug.parsed_input.intent)} />
              <KV k="dice_expression" v={mono(debug.parsed_input.dice_expression)} />
              <KV k="entities" v={mono(debug.parsed_input.entities)} />
            </>
          ) : (
            <span className="text-ink-subtle">no parsed_input</span>
          )}
        </Section>
      ) : null}

      {adjudication ? (
        <Section title="Adjudication">
          <KV k="success" v={mono(adjudication.success)} />
          <KV k="dice_result" v={mono(adjudication.dice_result)} />
          <KV k="damage" v={mono(adjudication.damage)} />
          <KV k="scene_id" v={mono(adjudication.scene_id)} />
          <div className="mt-1">
            <div className="text-ink-subtle">mechanical_summary</div>
            <pre className="mt-0.5 whitespace-pre-wrap break-words rounded bg-surface-sunken p-2 font-mono text-ink/85">
              {adjudication.mechanical_summary || "—"}
            </pre>
          </div>
          <div className="mt-1 border-t border-line pt-1">
            <div className="mb-0.5 text-ink-subtle">character_update</div>
            <KV k="hp_delta" v={mono(cu?.hp_delta)} />
            <KV k="add_conditions" v={list(cu?.add_conditions)} />
            <KV k="remove_conditions" v={list(cu?.remove_conditions)} />
            <KV k="inventory_add" v={list(cu?.inventory_add)} />
            <KV k="inventory_remove" v={list(cu?.inventory_remove)} />
          </div>
          <KV k="flags_set" v={mono(adjudication.flags_set)} />
          <KV k="flags_cleared" v={mono(adjudication.flags_cleared)} />
        </Section>
      ) : null}

      {debug ? (
        <Section
          title="RAG retrieval"
          warn={debug.rules_context_count === 0 || debug.adventure_context_count === 0}
        >
          <ChunkList
            label={`Rules (count ${debug.rules_context_count})`}
            chunks={debug.rules_context}
          />
          <ChunkList
            label={`Adventure (count ${debug.adventure_context_count})`}
            chunks={debug.adventure_context}
          />
        </Section>
      ) : null}

      {debug ? (
        <Section title="World flags (in)" defaultOpen={false}>
          {debug.world_flags_in && Object.keys(debug.world_flags_in).length > 0 ? (
            <div className="font-mono">
              {Object.entries(debug.world_flags_in).map(([k, v]) => (
                <KV key={k} k={k} v={mono(v)} />
              ))}
            </div>
          ) : (
            <span className="text-ink-subtle">none</span>
          )}
        </Section>
      ) : null}

      {debug ? (
        <Section title="Combat state" defaultOpen={false}>
          {debug.combat_state ? (
            <CombatBlock combat={debug.combat_state} />
          ) : (
            <span className="text-ink-subtle">no active combat</span>
          )}
        </Section>
      ) : null}

      {debug ? (
        <Section title="Timings & model" defaultOpen={false}>
          <KV k="parsing_ms" v={timing(timings.parsing_ms)} />
          <KV k="retrieving_ms" v={timing(timings.retrieving_ms)} />
          <KV k="adjudicating_ms" v={timing(timings.adjudicating_ms)} />
          <KV k="narrating_ms" v={timing(timings.narrating_ms)} />
          <KV k="total_ms" v={timing(timings.total_ms)} />
          <KV k="model" v={mono(debug.model)} />
        </Section>
      ) : null}

      {debug ? (
        <Section title="Latency, usage & cost" defaultOpen={false}>
          {debug.turn_diagnostics ? (
            <TurnDiagnosticsBlock diag={debug.turn_diagnostics} />
          ) : (
            <span className="text-ink-subtle">{UNKNOWN}: not captured for this turn</span>
          )}
        </Section>
      ) : null}

      <Section title="Raw debug JSON" defaultOpen={false}>
        <details>
          <summary className="cursor-pointer text-ink-subtle">Show raw JSON</summary>
          <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-sunken p-2 font-mono text-[11px] text-ink/85">
            {JSON.stringify(
              { debug: debug ?? null, adjudication: adjudication ?? null, trace: stored ?? null },
              null,
              2,
            )}
          </pre>
        </details>
      </Section>
    </div>
  );
}
