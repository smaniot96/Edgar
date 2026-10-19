/**
 * Frontend side of the turn stream contract (EDG-058).
 *
 * `stream.schema.json` is generated from the backend models in `apps/api/schemas/stream.py`
 * (`.venv/bin/python -m tools.generate_stream_schema`). These tests fail when the TypeScript
 * consumers drift from it:
 *  - each TypeScript key set is listed as an exhaustive `Record<keyof T, true>`, so adding a field
 *    to a TS interface without a matching literal is a compile error;
 *  - the backend fields and the TS fields are compared both ways, and every difference must be
 *    listed in an explicit allowlist with a reason, so a new field on either side fails here;
 *  - the event names, stage names and the dispatch in `consumeTurnStream` must agree with the
 *    schema, and unknown extra fields must still be tolerated.
 */
import { describe, expect, it } from "vitest";

import schemaJson from "./stream.schema.json";
import { consumeTurnStream, TURN_STATUS_STAGES, TURN_STREAM_EVENTS } from "./turnStream";
import type { TurnStreamOutcome } from "./turnStream";
import type {
  AdjudicationResult,
  CombatState,
  Combatant,
  DebugPayload,
  TurnCharacter,
  TurnResult,
} from "./types";

interface JsonNode {
  $ref?: string;
  anyOf?: JsonNode[];
  enum?: string[];
  items?: JsonNode;
  properties?: Record<string, JsonNode>;
  required?: string[];
  $defs?: Record<string, JsonNode>;
}

const schema = schemaJson as unknown as {
  events: string[];
  statusStages: string[];
  frames: Record<string, JsonNode>;
  publicModels: Record<string, JsonNode>;
};

/** Resolve a `$ref`/`anyOf` node against the document it was declared in. */
function resolve(root: JsonNode, node: JsonNode): JsonNode {
  if (node.$ref) {
    const name = node.$ref.replace("#/$defs/", "");
    const target = root.$defs?.[name];
    if (!target) throw new Error(`unresolved schema reference ${node.$ref}`);
    return resolve(root, target);
  }
  if (node.anyOf) {
    const object = node.anyOf.find((option) => option.$ref || option.properties);
    return object ? resolve(root, object) : node;
  }
  return node;
}

function frameRoot(event: string): JsonNode {
  const root = schema.frames[event];
  if (!root) throw new Error(`schema has no frame ${event}`);
  return root;
}

function frameKeys(event: string): string[] {
  return Object.keys(frameRoot(event).properties ?? {}).sort();
}

/** Keys of a nested object property of a frame (e.g. `done.combat_state`), resolved by reference. */
function nestedKeys(event: string, property: string): string[] {
  const root = frameRoot(event);
  const node = resolve(root, root.properties?.[property] ?? {});
  return Object.keys(node.properties ?? {}).sort();
}

interface KeyAllowlist {
  /** Declared by the backend, not modelled by the client type. Must say why. */
  backendOnly?: Record<string, string>;
  /** Declared by the client type, not sent by the backend contract. Must say why. */
  clientOnly?: Record<string, string>;
}

/**
 * Compare a backend key set with a client key set. Returns human-readable mismatches, empty when
 * every difference is allowlisted. A stale allowlist entry (listed but no longer different) is a
 * mismatch too, so the allowlists cannot quietly grow.
 */
function compareKeys(
  label: string,
  backend: readonly string[],
  client: readonly string[],
  allow: KeyAllowlist = {},
): string[] {
  const problems: string[] = [];
  const backendOnly = new Set(backend.filter((key) => !client.includes(key)));
  const clientOnly = new Set(client.filter((key) => !backend.includes(key)));
  for (const key of backendOnly) {
    if (!(key in (allow.backendOnly ?? {}))) problems.push(`${label}: backend sends ${key}, client type lacks it`);
  }
  for (const key of clientOnly) {
    if (!(key in (allow.clientOnly ?? {}))) problems.push(`${label}: client type declares ${key}, backend does not send it`);
  }
  for (const key of Object.keys(allow.backendOnly ?? {})) {
    if (!backendOnly.has(key)) problems.push(`${label}: stale backendOnly allowlist entry ${key}`);
  }
  for (const key of Object.keys(allow.clientOnly ?? {})) {
    if (!clientOnly.has(key)) problems.push(`${label}: stale clientOnly allowlist entry ${key}`);
  }
  return problems;
}

// --- Exhaustive client key sets: adding a field to a TS interface fails to compile until it is listed.

const TURN_RESULT_KEYS: Record<keyof TurnResult, true> = {
  event_id: true,
  citations: true,
  ruling_citation_ids: true,
  retrieval_degraded: true,
  source_status: true,
  source_mode: true,
  narration: true,
  character: true,
  current_scene_id: true,
  combat_state: true,
  campaign_complete: true,
  roll_status: true,
  roll: true,
};

const COMBAT_STATE_KEYS: Record<keyof CombatState, true> = {
  round: true,
  ended: true,
  outcome: true,
  second_wind_used: true,
  economy: true,
  pending_declaration: true,
  standing_orders: true,
  second_wind_uses_remaining: true,
  initiative_order: true,
  initiative: true,
  current_turn_id: true,
  current_turn_index: true,
  current_turn: true,
  player_ac: true,
  party: true,
  current_turn_character_id: true,
  combatants: true,
};

const COMBATANT_KEYS: Record<keyof Combatant, true> = {
  id: true,
  name: true,
  display_name: true,
  is_player: true,
  hp_current: true,
  hp_max: true,
  ac: true,
  attack_bonus: true,
  damage_dice: true,
  alive: true,
  disposition: true,
  can_act: true,
  effects: true,
  band: true,
  reaction_used: true,
  character_id: true,
  death_saves: true,
  fled: true,
};

const TURN_CHARACTER_KEYS: Record<keyof TurnCharacter, true> = {
  id: true,
  name: true,
  hp_current: true,
  hp_max: true,
  level: true,
  class: true,
  character_class: true,
  stats: true,
  inventory: true,
  second_wind: true,
  spell_slots: true,
  hit_dice: true,
};

const ADJUDICATION_KEYS: Record<keyof AdjudicationResult, true> = {
  citations: true,
  ruling_citation_ids: true,
  success: true,
  damage: true,
  conditions: true,
  mechanical_summary: true,
  dice_result: true,
  check: true,
  dc: true,
  against: true,
  modifier: true,
  roll_expression: true,
  natural_roll: true,
  dice_log: true,
  completion_status: true,
  rest_record: true,
  character_update: true,
  flags_set: true,
  flags_cleared: true,
  scene_id: true,
};

const DEBUG_KEYS: Record<keyof DebugPayload, true> = {
  parsed_input: true,
  rules_context: true,
  adventure_context: true,
  rules_context_count: true,
  adventure_context_count: true,
  world_flags_in: true,
  combat_state: true,
  timings_ms: true,
  combat_diagnostics: true,
  turn_diagnostics: true,
  model: true,
};

/** Fields the client reads from an `error` frame (see `safeTurnError`). */
const ERROR_FIELDS_READ = ["code", "category", "recovery_action", "retryable", "outcome", "support_ref"];
/** Fields the backend sends on `error` that the client deliberately does not read. */
const ERROR_FIELDS_IGNORED = ["message", "detail", "retrieval_degraded"];

const clientKeys = (record: Record<string, true>) => Object.keys(record).sort();

describe("stream schema and TypeScript consumers", () => {
  it("lists the same event names, in the same order, as the dispatcher", () => {
    expect(compareKeys("events", schema.events, [...TURN_STREAM_EVENTS])).toEqual([]);
    expect([...TURN_STREAM_EVENTS]).toEqual(schema.events);
  });

  it("lists the same status stages as the client", () => {
    expect([...TURN_STATUS_STAGES]).toEqual(schema.statusStages);
    const stageNode = resolve(frameRoot("status"), frameRoot("status").properties?.stage ?? {});
    expect(stageNode.enum).toEqual(schema.statusStages);
  });

  it("has a frame for every event the client dispatches", () => {
    expect(Object.keys(schema.frames).sort()).toEqual([...TURN_STREAM_EVENTS].sort());
  });

  it("matches the done frame to TurnResult, allowing only the documented backend-only field", () => {
    expect(
      compareKeys("done", frameKeys("done"), clientKeys(TURN_RESULT_KEYS), {
        backendOnly: {
          rules_edition: "engine edition label; the client reads roll and citations, not this field",
        },
      }),
    ).toEqual([]);
  });

  it("matches done.combat_state to CombatState", () => {
    expect(
      compareKeys("done.combat_state", nestedKeys("done", "combat_state"), clientKeys(COMBAT_STATE_KEYS), {
        backendOnly: {
          solo_budget: "encounter budget view; the HUD does not render it yet",
          raging: "barbarian rage flag; not declared on the client type yet",
          concentration: "concentrated spell name; not declared on the client type yet",
        },
        clientOnly: {
          initiative_order: "legacy roster order; the public view sends initiative instead",
        },
      }),
    ).toEqual([]);
  });

  it("matches a combatant in done.combat_state to Combatant", () => {
    const combatants = resolve(frameRoot("done"), frameRoot("done").properties?.combat_state ?? {});
    const itemRef = combatants.properties?.combatants?.items ?? {};
    const combatant = resolve(frameRoot("done"), itemRef);
    expect(
      compareKeys("done.combat_state.combatants[]", Object.keys(combatant.properties ?? {}).sort(), clientKeys(COMBATANT_KEYS), {
        clientOnly: {
          display_name: "older payloads send the name only; the backend folds display_name into name",
          attack_bonus: "attack view is not part of the public combat projection",
          damage_dice: "damage view is not part of the public combat projection",
          effects: "condition view is not part of the public combat projection",
        },
      }),
    ).toEqual([]);
  });

  it("matches done.character to TurnCharacter, including the resource views", () => {
    expect(
      compareKeys("done.character", nestedKeys("done", "character"), clientKeys(TURN_CHARACTER_KEYS), {
        backendOnly: {
          class_features: "per-feature uses (EDG-145); the client does not render them yet",
        },
        clientOnly: {
          character_class: "legacy alias of class; the client still accepts it for older payloads",
        },
      }),
    ).toEqual([]);
  });

  it("matches the adjudication frame to AdjudicationResult", () => {
    expect(
      compareKeys("adjudication", frameKeys("adjudication"), clientKeys(ADJUDICATION_KEYS), {
        backendOnly: {
          citation_ids: "exact reference ids for the ruling; the client reads citations",
        },
        clientOnly: {
          ruling_citation_ids:
            "declared on AdjudicationResult but the adjudication frame never sends it; the done frame carries ruling ids",
        },
      }),
    ).toEqual([]);
  });

  it("matches the debug frame to DebugPayload", () => {
    expect(
      compareKeys("debug", frameKeys("debug"), clientKeys(DEBUG_KEYS), {
        backendOnly: {
          model_profiles: "configured model profile snapshot for developer diagnostics; not displayed yet",
        },
      }),
    ).toEqual([]);
  });

  it("reads and ignores every error field the backend declares, and nothing else", () => {
    const declared = frameKeys("error");
    expect(ERROR_FIELDS_READ.filter((key) => !declared.includes(key))).toEqual([]);
    expect(declared.filter((key) => !ERROR_FIELDS_READ.includes(key) && !ERROR_FIELDS_IGNORED.includes(key))).toEqual([]);
  });

  it("reads the status stage and token text in the frame shape the schema declares", () => {
    expect(frameRoot("status").required).toEqual(["stage"]);
    expect(frameRoot("token").required).toEqual(["text"]);
  });
});

describe("detecting drift", () => {
  it("reports a backend field the client type does not declare", () => {
    const problems = compareKeys("done", [...frameKeys("done"), "new_backend_field"], clientKeys(TURN_RESULT_KEYS), {
      backendOnly: { rules_edition: "documented" },
    });
    expect(problems).toEqual(["done: backend sends new_backend_field, client type lacks it"]);
  });

  it("reports a client field the backend does not send", () => {
    const problems = compareKeys("done", frameKeys("done"), [...clientKeys(TURN_RESULT_KEYS), "ghost"], {
      backendOnly: { rules_edition: "documented" },
    });
    expect(problems).toEqual(["done: client type declares ghost, backend does not send it"]);
  });

  it("reports an allowlist entry that no longer describes a real difference", () => {
    const problems = compareKeys("done", frameKeys("done"), clientKeys(TURN_RESULT_KEYS), {
      backendOnly: { rules_edition: "documented", gone: "stale" },
    });
    expect(problems).toEqual(["done: stale backendOnly allowlist entry gone"]);
  });

  it("reports an event added to the backend contract that the dispatcher does not handle", () => {
    expect(compareKeys("events", [...schema.events, "trace"], [...TURN_STREAM_EVENTS])).toEqual([
      "events: backend sends trace, client type lacks it",
    ]);
  });
});

describe("consumeTurnStream dispatches every schema event", () => {
  const encoder = new TextEncoder();
  const sample: Record<string, Record<string, unknown>> = {
    status: { stage: "parsing" },
    token: { text: "The door opens. " },
    adjudication: { success: true, damage: null, mechanical_summary: "", dice_result: null },
    debug: { model: "stub" },
    done: { narration: "The door opens.", campaign_complete: false, roll: null },
    error: { code: "provider_unavailable", category: "provider_unavailable", recovery_action: "check_existing_result", outcome: "unknown" },
  };

  async function run(frames: string) {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(frames));
        controller.close();
      },
    });
    const seen: string[] = [];
    const outcome: TurnStreamOutcome = await consumeTurnStream(new Response(body), {
      onStatus: (stage) => seen.push(`status:${stage}`),
      onToken: (text) => seen.push(`token:${text}`),
      onAdjudication: () => seen.push("adjudication"),
      onDebug: () => seen.push("debug"),
    });
    return { outcome, seen };
  }

  it("has a sample frame for every schema event", () => {
    expect(Object.keys(sample).sort()).toEqual([...schema.events].sort());
  });

  for (const event of schema.events) {
    it(`handles ${event} and tolerates an unknown extra field`, async () => {
      const payload = { ...sample[event], future_field: { anything: true } };
      const frame = `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
      const { outcome, seen } = await run(frame);
      if (event === "done") expect(outcome.kind).toBe("done");
      else if (event === "error") expect(outcome.kind).toBe("error");
      else {
        expect(seen.length).toBe(1);
        expect(seen[0]?.startsWith(event === "status" ? "status:" : event)).toBe(true);
        expect(outcome.kind).toBe("incomplete");
      }
    });
  }

  it("passes status stages through unchanged for every stage in the schema", async () => {
    for (const stage of schema.statusStages) {
      const { seen } = await run(`event: status\ndata: ${JSON.stringify({ stage })}\n\n`);
      expect(seen).toEqual([`status:${stage}`]);
    }
  });
});
