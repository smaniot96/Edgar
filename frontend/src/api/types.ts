/** Mirrors FastAPI response shapes used by the UI. */

export interface UserRead {
  id: number;
  email: string;
  display_name: string | null;
  created_at: string;
}

export type AdventureStatus = "processing" | "ready" | "failed";

export type AdventureSize = "small" | "medium" | "large" | "gigantic";

export interface AdventureGenerateRequest {
  title?: string;
  theme?: string;
  size: AdventureSize;
}

export interface AdventureGenerateResponse {
  slug: string;
  title: string;
  status: "processing";
}

export interface AdventureRead {
  slug: string;
  title: string;
  description: string | null;
  level_range: string | null;
  cover_image: string | null;
  status?: AdventureStatus;
  chunks?: number | null;
  source_filename?: string | null;
  error?: string | null;
  created_at?: string | null;
}

export interface CharacterAssignmentRef {
  campaign_id: number;
  campaign_title: string;
}

export interface CharacterListItem {
  id: number;
  name: string;
  character_class: string;
  level: number;
  hp_max: number;
  base_stats: Record<string, unknown>;
  base_inventory: Record<string, unknown>;
  created_at: string;
  current_assignment: CharacterAssignmentRef | null;
}

export interface CharacterPreset {
  key: string;
  label: string;
  character_class: string;
  level: number;
  hp_max: number;
  hp_current: number;
  stats: Record<string, number>;
  inventory: Record<string, unknown>;
}

export interface CampaignCharacterRead {
  assignment_id: number;
  character_id: number;
  name: string;
  character_class: string;
  level: number;
  hp_current: number;
  hp_max: number;
}

export interface NPCRead {
  id: number;
  campaign_id: number;
  name: string;
  disposition: string;
  stat_block: Record<string, unknown>;
}

export interface WorldFlagRead {
  id: number;
  campaign_id: number;
  key: string;
  value: string;
}

export interface SeedResponse {
  user_id: number;
  campaign_id: number;
  session_id: number;
  message: string;
}

export interface SessionRead {
  id: number;
  campaign_id: number;
  started_at: string;
  ended_at: string | null;
  current_scene_id: string | null;
  active_character_id: number | null;
}

export interface ChatMessageRead {
  role: "user" | "dm";
  content: string;
  created_at: string;
}

export interface CampaignRead {
  id: number;
  title: string;
  system: string;
  created_by: number;
  created_at: string;
  adventure_collections: string[];
  status: "active" | "ended";
  ended_at: string | null;
}

export interface CharacterRead {
  id: number;
  owner_user_id: number;
  name: string;
  character_class: string;
  level: number;
  hp_max: number;
  base_stats: Record<string, unknown>;
  base_inventory: Record<string, unknown>;
  created_at: string;
}

export interface CharacterAssignmentHistoryRead {
  id: number;
  campaign_id: number;
  campaign_title: string;
  hp_current: number;
  hp_max: number;
  stats: Record<string, unknown>;
  inventory: Record<string, unknown>;
  assigned_at: string;
  ended_at: string | null;
  status: string;
}

/** Per-turn adjudication frame from the SSE/turn contract. */
export interface CharacterUpdate {
  hp_delta?: number | null;
  add_conditions?: string[] | null;
  remove_conditions?: string[] | null;
  inventory_add?: string[] | null;
  inventory_remove?: string[] | null;
  /** Dice expression the damage came from (e.g. "1d8+3"). */
  damage_dice?: string | null;
  [key: string]: unknown;
}

export interface FlagUpdate {
  key: string;
  value: string;
}

export interface DiceLogEntry {
  expression: string;
  rolls: number[];
  modifier: number;
  total: number;
  purpose?: string | null;
}

export interface AdjudicationResult {
  success: boolean;
  damage: number | null;
  conditions?: string[] | null;
  mechanical_summary: string;
  /** Total of the rolled expression; null when no dice were rolled. */
  dice_result: number | null;
  /** Roll context: check name, target number (DC, or the AC for attacks), modifier, expression. */
  check?: string | null;
  dc?: number | null;
  against?: "DC" | "AC" | null;
  modifier?: number | null;
  /** e.g. "1d20+5". */
  roll_expression?: string | null;
  /** The raw d20 face (for nat 20 / nat 1). */
  natural_roll?: number | null;
  dice_log?: DiceLogEntry[] | null;
  /** "pending" = the DM thinks the adventure may be over and awaits the player's confirmation. */
  completion_status?: "none" | "pending" | "accepted" | "rejected" | null;
  character_update?: CharacterUpdate | null;
  flags_set?: FlagUpdate[] | null;
  flags_cleared?: string[] | null;
  scene_id?: string | null;
}

export interface Combatant {
  name: string;
  display_name?: string | null;
  is_player: boolean;
  hp_current: number;
  hp_max: number;
  ac?: number | null;
  attack_bonus?: number | null;
  damage_dice?: string | null;
  alive: boolean;
}

export type CombatOutcome = "victory" | "defeat" | "fled" | null;

/** Initiative entry (newer backends); `init` is the rolled initiative. */
export interface InitiativeEntry {
  name: string;
  init?: number | null;
  is_player?: boolean;
  hp?: number | null;
  hp_max?: number | null;
  ac?: number | null;
  alive?: boolean;
}

export interface CombatState {
  round: number;
  ended: boolean;
  outcome: CombatOutcome;
  second_wind_used?: boolean;
  initiative_order?: string[];
  initiative?: InitiativeEntry[] | null;
  /** Index into the initiative order of whose turn it is (if provided). */
  current_turn_index?: number | null;
  /** Name of the combatant whose turn it is (if provided). */
  current_turn?: string | null;
  player_ac?: number | null;
  combatants: Combatant[];
}

/** Single retrieved RAG chunk surfaced in dev mode. */
export interface RetrievedChunk {
  text: string;
  source: string;
  page: number | null;
  collection: string;
  score: number;
}

export interface ParsedInput {
  intent: "combat" | "rp" | "exploration" | string;
  entities: Record<string, unknown>;
  dice_expression: string | null;
}

export interface DebugTimings {
  parsing_ms?: number;
  retrieving_ms?: number;
  adjudicating_ms?: number;
  narrating_ms?: number;
  total_ms?: number;
  [key: string]: number | undefined;
}

/** Payload of the optional `debug` SSE event, emitted only when debug=true. */
export interface DebugPayload {
  parsed_input: ParsedInput | null;
  rules_context: RetrievedChunk[];
  adventure_context: RetrievedChunk[];
  rules_context_count: number;
  adventure_context_count: number;
  world_flags_in: Record<string, unknown>;
  combat_state: CombatState | null;
  timings_ms: DebugTimings;
  model: string;
}

/**
 * Character carried by the `done`/sync turn payload. The backend sends `class`; older
 * builds/types used `character_class` — accept both.
 */
export interface TurnCharacter {
  id?: number;
  name?: string;
  hp_current?: number;
  hp_max?: number;
  level?: number;
  class?: string;
  character_class?: string;
  stats?: Record<string, unknown> | null;
  inventory?: Record<string, unknown> | null;
}

/** Shape of the `done` SSE frame and the sync POST turn response. */
export interface TurnResult {
  narration?: string;
  character?: TurnCharacter | null;
  current_scene_id?: string | null;
  combat_state?: CombatState | null;
  campaign_complete?: boolean;
}

/** Error payload of a failed turn (SSE `error` frame or HTTP error body). */
export interface TurnErrorPayload {
  code?: string;
  message?: string;
  detail?: string | { code?: string; message?: string } | unknown;
}
