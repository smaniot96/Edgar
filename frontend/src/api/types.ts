/** Mirrors FastAPI response shapes used by the UI. */

export interface UserRead {
  id: number;
  email: string;
  display_name: string | null;
  created_at: string;
}

export type AdventureStatus = "processing" | "ready" | "failed" | "missing" | "empty" | "partial" | "unavailable";

/** Host-only review draft for a legacy (non-private) adventure; text is the host's own source. */
export interface ManifestInspection {
  manifest: Record<string, unknown> & { passages: Array<{ passage_id: string; audience: string }>; opening_scene_id: string | null };
  reviewed_digest: string;
  passages: Array<{ passage_id: string; text: string }>;
  review_note: string;
}

/** Spoiler-safe solo review (EDG-176). Shared campaigns receive only `mode`. Never passage text or identifiers. */
export interface SoloAdventureReview {
  mode: "solo" | "shared";
  state: "no_draft" | "blocked" | "needs_classification" | "ready" | "published" | null;
  title: string | null;
  rules_edition: string | null;
  checks: Array<{
    code: string;
    status: "passed" | "failed";
    message: string;
    count: number | null;
    recovery: "regenerate" | "manual_review" | null;
  }>;
  warnings: Array<{ code: string; message: string }>;
  counts: {
    passages: number;
    player: number;
    planner: number;
    excluded: number;
    unreviewed: number;
    scenes: number;
    entities: number;
    resolution_policies: number;
    coverage_percent: number;
  } | null;
  reviewed_digest: string | null;
  reviewer: string | null;
}

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
  job?: AdventureJobRead | null;
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

export interface AdventureJobRead {
  version: 1;
  job_id: string;
  kind: "generation" | "ingestion";
  state: "queued" | "running" | "interrupted" | "failed" | "completed" | "cancel_requested" | "cancelled";
  stage: string;
  completed_chapters: number;
  total_chapters: number | null;
  checkpoint_available: boolean;
  retryable: boolean;
  retained_work: "none" | "outline_and_chapters" | "complete_manuscript" | "source_pdf" | "inspected_extraction" | "published";
  action_label: string | null;
  explanation: string | null;
  source_version: string | null;
  failure?: SafeFailureRead | null;
}

export interface SafeFailureRead {
  code: string;
  category: "source" | "review" | "validation" | "assignment" | "busy" | "provider_unavailable" | "rate_limited" | "quota_exceeded" | "provider_configuration" | "cancelled" | "interrupted" | "internal" | "not_found";
  message: string;
  recovery_action: "repair_source" | "review_source" | "fix_input" | "assign_character" | "wait" | "retry" | "check_existing_result" | "configure_provider" | "restart_preparation" | "contact_support" | "read_history";
  retryable: boolean;
  support_ref: string;
  outcome: "not_committed" | "committed" | "unknown";
}

export interface ExtractionPreview extends IngestionSummary {
  source_current?: boolean;
  pages: { page: number; method: string; warnings: string[]; warning_messages?: string[]; quality: { errors: string[] } }[];
  previews: { kind: "text" | "stat" | "scene_candidate"; page: number; page_end: number; section: string | null; text: string }[];
}

export type AdventureSize = "small" | "medium" | "large" | "gigantic";

export interface PreparationSetup {
  character_ids: number[];
  target_level?: number;
  max_cost_microusd: number;
  required_maps: boolean;
  required_handouts: boolean;
}

export interface AdventureGenerateRequest {
  preparation?: PreparationSetup;
  rules_edition?: RulesEdition;
  title?: string;
  theme?: string;
  size: AdventureSize;
  /** Client identity for one generate attempt; a retry reuses it so the server cannot create a second module. */
  request_id?: string;
}

export interface AdventureGenerateResponse {
  slug: string;
  title: string;
  status: "processing";
}

export interface AdventureRead {
  readiness?: ReadinessRead | null;
  preparation_setup_required?: boolean;
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
  job?: AdventureJobRead | null;
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
  /** What the engine executes for this class, shown as a label on the class picker. */
  engine_support: string;
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
  /** Null identifies a campaign-wide manual note; otherwise this NPC belongs to one save. */
  session_id: number | null;
  name: string;
  disposition: string;
  stat_block: Record<string, unknown>;
  /** Opaque link to the same earned person entry used by the selected session's journal. */
  journal_entry_id: string | null;
}

export interface NPCListRead {
  npcs: NPCRead[];
  tracking: "not_selected" | "available" | "legacy_untracked";
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
  character_name?: string | null;
  has_history?: boolean;
  last_played_at?: string | null;
  readiness?: ReadinessRead | null;
}

export interface SessionMapPlace {
  scene_id: string;
  title: string;
  status: "current" | "visited" | "known";
  detail: string | null;
}

export interface SessionMapRoute {
  from_scene_id: string;
  to_scene_id: string;
}

export interface SessionMapAsset {
  asset_id: string;
  kind: string;
  url: string;
  media_type: string;
  sha256: string;
  entity_id: string | null;
  provenance: Record<string, unknown>;
  profile_settings: Record<string, unknown>;
}

export interface SessionMapRead {
  session_id: number;
  source_version: string;
  topology: "prepared" | "reviewed_manifest" | "unavailable";
  schematic: boolean;
  scale: null;
  current_scene_id: string | null;
  places: SessionMapPlace[];
  routes: SessionMapRoute[];
  note: string | null;
  journal_entry_ids: Record<string, string>;
  known_place_entries: Array<{ id: string; title: string; text: string | null; status: string; origin: Record<string, unknown> }>;
  artwork: SessionMapAsset[];
  tracking: "available" | "legacy_untracked";
}

/** Whether a DM line's original roll was stored (EDG-030). Legacy rows are "not_captured". */
export type RollStatus = "captured" | "no_roll" | "not_captured";

/** Engine evidence of a rest turn (EDG-026). Every number is engine-rolled or engine-derived. */
export interface RestRecord {
  kind: "short" | "long";
  /** A short rest with no Hit Dice left: nothing rolled and nothing healed. */
  refused: boolean;
  /** The short-rest roll, e.g. "1d10+2". */
  expression: string | null;
  /** The roll total before the cap at maximum HP. */
  rolled: number | null;
  /** HP actually regained, capped at the maximum. */
  healed: number;
  /** The class Hit Die, e.g. "d10". */
  die: string;
  hit_dice_remaining: number;
  hit_dice_total: number;
  /** Long rest only: Hit Dice regained. */
  hit_dice_recovered: number | null;
}

/** The sheet's Hit Dice (EDG-026): remaining and total dice of the class die. */
export interface HitDiceStatus {
  remaining: number;
  total: number;
  die: string;
}

/** The turn's original ruling and dice as stored at commit. Reload shows these; nothing re-rolls. */
export interface RollRecord {
  turn_id: string | null;
  check: string | null;
  success: boolean;
  dc: number | null;
  against: "DC" | "AC" | null;
  modifier: number | null;
  roll_expression: string | null;
  natural_roll: number | null;
  dice_result: number | null;
  damage: number | null;
  healing: number | null;
  conditions: string[];
  mechanical_summary: string;
  dice_log: DiceLogEntry[];
  /** Rest evidence for a rest turn (EDG-026); null for other turns. */
  rest?: RestRecord | null;
}

export interface ChatMessageRead {
  event_id?: number | null;
  citations?: SourceCitation[];
  ruling_citation_ids?: string[];
  role: "user" | "dm";
  content: string;
  /** DM lines only. */
  turn_id?: string | null;
  roll_status?: RollStatus | null;
  roll?: RollRecord | null;
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

export type ReadinessCode =
  | "ready"
  | "source_missing"
  | "source_unavailable"
  | "source_review_needed"
  | "adventure_review_needed"
  | "saved_version_conflict"
  | "preparation_in_progress"
  | "preparation_failed"
  | "rules_edition_mismatch"
  | "choose_character"
  | "character_assignment_conflict"
  | "completed"
  | "readiness_unavailable";

export type ReadinessAction =
  | "play"
  | "read_history"
  | "recover_source"
  | "review_source"
  | "review_adventure"
  | "wait_for_preparation"
  | "repair_preparation"
  | "choose_character"
  | "reopen_campaign"
  | "retry_readiness";

export interface ReadinessBlocker {
  code: ReadinessCode;
  message: string;
  action: ReadinessAction;
}

export interface ReadinessRead {
  play_allowed: boolean;
  history_allowed: boolean;
  state: ReadinessCode;
  primary_action: ReadinessAction;
  blockers: ReadinessBlocker[];
}

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
  readiness?: ReadinessRead | null;
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
  /** Derived Hit Dice view (EDG-026); absent on older servers. */
  hit_dice?: HitDiceStatus | null;
  assigned_at: string;
  ended_at: string | null;
  status: string;
  /** Derived Second Wind resource (EDG-034); absent for non-fighters and legacy payloads. */
  second_wind?: SecondWindState | null;
  /** Derived spell slots per level, keyed by level as a string (casters only). */
  spell_slots?: Record<string, SpellSlotState> | null;
  /** Engine armour class (EDG-173); absent on older servers. */
  armor_class?: number | null;
  /** Catalog spells the class can cast at its highest slot level (EDG-173); absent for non-casters. */
  spells?: CastableSpell[] | null;
  /** Proficient saving throws and skills with engine modifiers (EDG-173). */
  proficiencies?: SheetProficiencies | null;
}

/** A supported catalog spell the character can cast; `level` 0 is a cantrip (no slot). */
export interface CastableSpell {
  name: string;
  level: number;
  /** "action" or "bonus_action". */
  action: string;
}

/** Proficient saving throws (ability key) and skills (skill key), each with its engine modifier. */
export interface SheetProficiencies {
  saving_throws: { ability: string; modifier: number }[];
  skills: { skill: string; modifier: number }[];
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
  /** Rest evidence for a rest turn (EDG-026). */
  rest_record?: RestRecord | null;
  character_update?: CharacterUpdate | null;
  flags_set?: FlagUpdate[] | null;
  flags_cleared?: string[] | null;
  scene_id?: string | null;
}

/** Engine-owned Second Wind counter (read-only view of the sheet resource). */
export interface SecondWindState {
  uses_remaining: number;
  uses_max: number;
}

/** Engine-owned spell slot counter for one spell level. */
export interface SpellSlotState {
  total: number;
  remaining: number;
}

/** Supported combat effect on a combatant (EDG-021). Only the engine list is mechanical. */
export interface CombatEffect {
  kind: string;
  source?: string | null;
  applied_round?: number | null;
}

export interface Combatant {
  /** Stable roster identity supplied by the server; older saves may omit it. */
  id?: string;
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
  effects?: CombatEffect[] | null;
  /** A foe's distance band from the hero (EDG-044): "melee" or "ranged"; null for the hero or a legacy row. */
  band?: string | null;
  /** Whether the foe's reaction is spent (EDG-044, an opportunity attack this round). */
  reaction_used?: boolean | null;
}

export type CombatOutcome = "victory" | "defeat" | "fled" | null;

/** Initiative entry (newer backends); `init` is the rolled initiative. */
export interface InitiativeEntry {
  id?: string;
  name: string;
  init?: number | null;
  is_player?: boolean;
  hp?: number | null;
  hp_max?: number | null;
  ac?: number | null;
  alive?: boolean;
  disposition?: string | null;
  can_act?: boolean | null;
  effects?: CombatEffect[] | null;
  band?: string | null;
  reaction_used?: boolean | null;
}

/** What the hero's last turn spent, as the engine recorded it (EDG-044, additive). */
export interface CombatEconomy {
  round?: number | null;
  /** Label of the action the turn spent (e.g. "attack", "Dodge"), or null when none. */
  action?: string | null;
  /** Label of the bonus action the turn spent (e.g. "Second Wind"), or null when none. */
  bonus_action?: string | null;
  dodging?: boolean | null;
  /** The reaction the hero spent this round, e.g. "readied attack" (EDG-044c, additive); absent when unused. */
  reaction?: string | null;
}

export interface CombatState {
  round: number;
  ended: boolean;
  outcome: CombatOutcome;
  second_wind_used?: boolean;
  /** The hero's last turn (additive); absent or null for legacy payloads. */
  economy?: CombatEconomy | null;
  /** Fighter Second Wind uses left on the sheet (additive). */
  second_wind_uses_remaining?: number | null;
  initiative_order?: string[];
  initiative?: InitiativeEntry[] | null;
  /** Stable identity of the combatant whose turn it is; authoritative when provided. */
  current_turn_id?: string | null;
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

/**
 * One measured provider call of a DM turn (EDG-053). Every measured value is a number; a value
 * the provider did not report is `null`, which the UI shows as "unknown" and never as zero.
 */
export interface ProviderCallDiagnostics {
  kind: string;
  profile: string;
  requested_model: string | null;
  resolved_model: string | null;
  streamed: boolean;
  latency_ms: number | null;
  first_text_ms: number | null;
  attempts: number | null;
  error_category: string | null;
  usage: { input_tokens: number | null; output_tokens: number | null; total_tokens: number | null };
  cost_microusd: number | null;
}

/**
 * Developer view of one DM turn's measured latency, usage and cost (EDG-053). Scope is DM turns
 * only; infrastructure readiness probes are never part of it. `totals.complete` is false when any
 * call lacked usage or cost, so a total with unknown parts is labelled partial.
 */
export interface TurnDiagnostics {
  scope: "dm_turn";
  version: number | null;
  turn_id: string | null;
  path: "normal" | "combat" | null;
  delivery: "sync" | "stream" | null;
  total_ms: number | null;
  time_to_first_text_ms: number | null;
  stages_ms: Partial<Record<string, number | null>>;
  stages_unavailable: string[];
  lock: { wait_ms: number | null; contended: boolean; attempts: number | null } | null;
  retrieval: {
    latency_ms: number | null;
    rules_chunks: number | null;
    adventure_chunks: number | null;
    degraded: boolean;
  } | null;
  provider_calls: ProviderCallDiagnostics[];
  totals: {
    calls: number;
    calls_dropped: number;
    input_tokens: number | null;
    output_tokens: number | null;
    total_tokens: number | null;
    cost_microusd: number | null;
    calls_unknown_usage: number;
    calls_unknown_cost: number;
    complete: boolean;
  };
}

/** One retrieved chunk in a stored developer trace (EDG-138). Text only where the audience allows it. */
export interface TurnTraceChunk {
  collection: string | null;
  source: string | null;
  page: number | null;
  score: number | null;
  /** Present only for player-audience chunks; absent when `text_withheld` is true. */
  text?: string;
  text_withheld: boolean;
  citation: {
    reference_id?: string;
    rules_edition?: string;
    edition_status?: string;
    source_sha256?: string | null;
    extraction_sha256?: string | null;
    page?: number | null;
    page_end?: number | null;
    source_pages?: number[];
    audience?: string;
  };
}

export interface TurnTraceRag {
  count: number;
  chunks: TurnTraceChunk[];
  chunks_dropped: number;
}

/** Developer trace of one DM turn, persisted across reload (EDG-138). Whitelisted server-side. */
export interface TurnTrace {
  version: number;
  scope: "dm_turn";
  turn_id: string;
  delivery: "sync" | "stream";
  path: "normal" | "combat";
  parsed_input: {
    intent: string | null;
    check: string | null;
    difficulty: string | null;
    target: string | null;
    retrieval_query: string | null;
    uses_item: string | null;
    lethality: string | null;
    confirms_ending: boolean | null;
    entities: Record<string, string | number | boolean | null>;
  } | null;
  rag: { rules: TurnTraceRag; adventure: TurnTraceRag };
  settings: {
    rules_edition: string;
    source_mode: string;
    narrator_profile: string;
    profile_version: number | null;
    profiles: Record<string, { model: string | null; reasoning_effort: string | null; api: string | null }>;
  };
  /** Companion rows matched by turn_id at read time, not copied into the trace. */
  composed_with: string[];
}

/** One DM turn of the developer trace list. `trace` is null only for a row this build cannot read. */
export interface TurnTraceRecord {
  turn_id: string;
  created_at: string;
  trace: TurnTrace | null;
  turn_diagnostics: TurnDiagnostics | null;
  combat_round: CombatRoundDiagnostics | null;
}

/** GET /sessions/{id}/diagnostics/turn-traces (developer only). Turns absent here were not captured. */
export interface TurnTraceList {
  scope: "dm_turns";
  truncated: boolean;
  turns: TurnTraceRecord[];
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
  /** This turn's measured latency, usage and cost (EDG-053); absent on older builds. */
  turn_diagnostics?: TurnDiagnostics | null;
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
  /** Fighter Second Wind resource (additive; present for fighters). */
  second_wind?: SecondWindState | null;
  /** Derived spell slots per level (casters only; additive). */
  spell_slots?: Record<string, SpellSlotState> | null;
  /** Hit Dice resource (EDG-026; additive, present for every class). */
  hit_dice?: HitDiceStatus | null;
}

/** Shape of the `done` SSE frame and the sync POST turn response. */
export interface TurnResult {
  event_id?: number | null;
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
  roll_status?: RollStatus | null;
  roll?: RollRecord | null;
}
