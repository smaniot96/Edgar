/** Mirrors FastAPI response shapes used by the UI. */

export interface UserRead {
  id: number;
  email: string;
  display_name: string | null;
  created_at: string;
}

export interface AdventureRead {
  slug: string;
  title: string;
  description: string | null;
  level_range: string | null;
  cover_image: string | null;
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
