/** Mirrors FastAPI response shapes used by the UI. */

export interface UserRead {
  id: number;
  email: string;
  display_name: string | null;
  created_at: string;
}

export type AdventureStatus = "processing" | "ready" | "failed" | "missing" | "empty" | "partial" | "unavailable";

export interface SourceRead {
  slug: string;
  title: string;
  status: AdventureStatus;
  usable_chunks: number;
  expected_chunks: number | null;
  collection_exists: boolean;
  source_pdf_available: boolean;
  message: string | null;
  failure_code?: string | null;
  extraction_preview_available?: boolean;
  extraction_preview_current?: boolean;
  ingestion?: IngestionSummary | null;
}

export interface IngestionSummary {
  source_kind?: "pdf" | "generated" | "text";
  source_sha256: string;
  extraction_sha256: string;
  page_count: number;
  required_pages: number[];
  chunk_count: number;
  quality_passed: boolean;
  warning_count: number;
  review_note: string;
  issues: { code: string; page?: number; message: string }[];
}

export interface ExtractionPreview extends IngestionSummary {
  source_current?: boolean;
  pages: { page: number; method: string; warnings: string[]; warning_messages?: string[]; quality: { errors: string[] } }[];
  previews: { kind: "text" | "stat" | "scene_candidate"; page: number; page_end: number; section: string | null; text: string }[];
}

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
  rules_edition?: RulesEdition | "unspecified";
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
  source_pdf_available?: boolean;
  can_retry?: boolean;
  failure_code?: string | null;
  extraction_preview_available?: boolean;
  extraction_preview_current?: boolean;
  ingestion?: IngestionSummary | null;
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

export interface SessionRead {
  id: number;
  campaign_id: number;
  started_at: string;
  ended_at: string | null;
  current_scene_id: string | null;
  active_character_id: number | null;
}

export interface ChatMessageRead {
  citations?: SourceCitation[];
  ruling_citation_ids?: string[];
  role: "user" | "dm";
  content: string;
  created_at: string;
}

export interface SessionIntroRead {
  citations?: SourceCitation[];
  ruling_citation_ids?: string[];
  started: boolean;
  narration?: string;
  current_scene_id?: string | null;
}

export type RulesEdition = "2014" | "2024";
export interface SourceCitation {
  reference_id: string; source: string; rules_edition: RulesEdition | "unspecified";
  source_pages: number[]; source_sha256: string | null; extraction_sha256: string | null;
  edition_status: "compatible" | "incompatible" | "unverified";
}

export interface CampaignRead {
  rules_edition?: RulesEdition;
  id: number;
  title: string;
  system: string;
  created_by: number;
  created_at: string;
  adventure_collections: string[];
  source_mode?: "module" | "homebrew";
  sources?: SourceRead[];
  retrieval_degraded?: boolean;
  play_allowed?: boolean;
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
  /** Set on advantage/disadvantage d20 rolls: both faces are in `rolls`, `kept` is used. */
  roll_mode?: "advantage" | "disadvantage" | null;
  kept?: number | null;
}

export interface AdjudicationResult {
  citations?: SourceCitation[];
  ruling_citation_ids?: string[];
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
  /** Combat disposition from the engine: active, unconscious, captured, dead or defeated. */
  disposition?: string | null;
  can_act?: boolean | null;
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
  disposition?: string | null;
  can_act?: boolean | null;
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

/**
 * Developer view of one combat round (EDG-031). Stage timings are only those the round measured;
 * a stage listed in `timings_unavailable` was not measured and must not be shown as zero.
 */
export interface CombatRoundDiagnostics {
  turn_id: string | null;
  round: number;
  ended: boolean;
  outcome: string | null;
  actors: { id: string; name: string }[];
  player_action: Record<string, unknown>;
  outcomes: Record<string, unknown>[];
  dispositions: Record<string, unknown>[];
  dice_log: Record<string, unknown>[];
  timings_ms: Record<string, number>;
  timings_unavailable: string[];
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
  /** Combat turns only; null for a non-combat turn. */
  combat_diagnostics?: CombatRoundDiagnostics | null;
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
  citations?: SourceCitation[];
  ruling_citation_ids?: string[];
  retrieval_degraded?: boolean;
  source_status?: SourceRead[];
  source_mode?: "module" | "homebrew";
  narration?: string;
  character?: TurnCharacter | null;
  current_scene_id?: string | null;
  combat_state?: CombatState | null;
  campaign_complete?: boolean;
}
