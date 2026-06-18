import { useState } from "react";
import type { ReactNode } from "react";

import type {
  AdjudicationResult,
  CombatState,
  DebugPayload,
  RetrievedChunk,
} from "../api/types";
import { cn } from "../lib/cn";

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
    <div className="border-t border-[#2a2d33] first:border-t-0">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={cn(
          "flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs font-semibold uppercase tracking-wide",
          warn ? "text-amber-300" : "text-[#9a9a9a]",
        )}
      >
        <span className="text-[#6a6a6a]">{open ? "▾" : "▸"}</span>
        <span>{title}</span>
      </button>
      {open ? <div className="px-3 pb-3 pt-0">{children}</div> : null}
    </div>
  );
}

function KV({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex gap-2 py-0.5">
      <span className="min-w-[140px] shrink-0 text-[#8a8a8a]">{k}</span>
      <span className="break-words font-mono text-[#d7d7d7]">{v}</span>
    </div>
  );
}

function mono(v: unknown): ReactNode {
  if (v === null || v === undefined) return <span className="text-[#6a6a6a]">—</span>;
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function list(arr: unknown): ReactNode {
  if (!Array.isArray(arr) || arr.length === 0) return <span className="text-[#6a6a6a]">—</span>;
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
            : "bg-[#1a1c20] text-[#9a9a9a]",
        )}
      >
        {label}: {empty ? "0 retrieved ⚠" : `${chunks.length} retrieved`}
      </div>
      {empty ? null : (
        <ol className="flex flex-col gap-1.5">
          {chunks.map((c, i) => (
            <li key={i} className="rounded border border-[#2a2d33] bg-[#15161a] p-2 text-xs">
              <div className="mb-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[#8a8a8a]">
                <span>#{i + 1}</span>
                <span>{c.collection}</span>
                <span>score {typeof c.score === "number" ? c.score.toFixed(3) : "—"}</span>
                <span>{c.source}</span>
                {c.page != null ? <span>p.{c.page}</span> : null}
              </div>
              <div className="whitespace-pre-wrap break-words font-mono text-[#c7c7c7]">{c.text}</div>
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
          <thead className="text-[#8a8a8a]">
            <tr>
              {["name", "player", "hp", "ac", "atk", "dmg", "alive"].map((h) => (
                <th key={h} className="border-b border-[#2a2d33] px-1.5 py-1 font-semibold">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="text-[#d7d7d7]">
            {combat.combatants.map((c, i) => (
              <tr key={i} className={cn(!c.alive && "text-[#6a6a6a] line-through")}>
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

/**
 * Collapsible developer panel rendered under a DM message in dev mode. Shows
 * everything captured for the turn so correctness can be verified at a glance.
 */
export function DevPanel({
  debug,
  adjudication,
}: {
  debug?: DebugPayload | null;
  adjudication?: AdjudicationResult | null;
}) {
  if (!debug && !adjudication) {
    return (
      <div className="mr-auto mt-1 max-w-[820px] rounded-md border border-amber-900/60 bg-[#15161a] px-3 py-2 text-xs italic text-[#8a8a8a]">
        🔧 No debug captured for this turn (sent in User mode).
      </div>
    );
  }

  const cu = adjudication?.character_update ?? null;
  const timings = debug?.timings_ms ?? {};

  return (
    <div className="mr-auto mt-1 w-full max-w-[820px] overflow-hidden rounded-md border border-amber-900/60 bg-[#101216] text-xs">
      <div className="flex items-center gap-2 border-b border-amber-900/60 bg-amber-950/30 px-3 py-1.5 font-semibold text-amber-200">
        <span>🔧 Dev panel</span>
        {debug?.model ? (
          <span className="ml-auto font-mono text-[#9a9a9a]">{debug.model}</span>
        ) : null}
      </div>

      {debug?.parsed_input !== undefined ? (
        <Section title="Intent">
          {debug.parsed_input ? (
            <>
              <KV k="intent" v={mono(debug.parsed_input.intent)} />
              <KV k="dice_expression" v={mono(debug.parsed_input.dice_expression)} />
              <KV k="entities" v={mono(debug.parsed_input.entities)} />
            </>
          ) : (
            <span className="text-[#6a6a6a]">no parsed_input</span>
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
            <div className="text-[#8a8a8a]">mechanical_summary</div>
            <pre className="mt-0.5 whitespace-pre-wrap break-words rounded bg-[#15161a] p-2 font-mono text-[#c7c7c7]">
              {adjudication.mechanical_summary || "—"}
            </pre>
          </div>
          <div className="mt-1 border-t border-[#2a2d33] pt-1">
            <div className="mb-0.5 text-[#8a8a8a]">character_update</div>
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
            <span className="text-[#6a6a6a]">none</span>
          )}
        </Section>
      ) : null}

      {debug ? (
        <Section title="Combat state" defaultOpen={false}>
          {debug.combat_state ? (
            <CombatBlock combat={debug.combat_state} />
          ) : (
            <span className="text-[#6a6a6a]">no active combat</span>
          )}
        </Section>
      ) : null}

      {debug ? (
        <Section title="Timings & model" defaultOpen={false}>
          <KV k="parsing_ms" v={mono(timings.parsing_ms)} />
          <KV k="retrieving_ms" v={mono(timings.retrieving_ms)} />
          <KV k="adjudicating_ms" v={mono(timings.adjudicating_ms)} />
          <KV k="narrating_ms" v={mono(timings.narrating_ms)} />
          <KV k="total_ms" v={mono(timings.total_ms)} />
          <KV k="model" v={mono(debug.model)} />
        </Section>
      ) : null}

      <Section title="Raw debug JSON" defaultOpen={false}>
        <details>
          <summary className="cursor-pointer text-[#8a8a8a]">Show raw JSON</summary>
          <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-[#15161a] p-2 font-mono text-[11px] text-[#c7c7c7]">
            {JSON.stringify({ debug: debug ?? null, adjudication: adjudication ?? null }, null, 2)}
          </pre>
        </details>
      </Section>
    </div>
  );
}
