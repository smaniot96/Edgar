/** Normalise backend character / combat payloads into what the play UI renders. */
import type {
  CharacterAssignmentHistoryRead,
  CharacterRead,
  CombatState,
  TurnCharacter,
} from "../../api/types";

export interface InventoryGroup {
  label: string;
  items: string[];
}

export interface SheetCharacter {
  id: number | null;
  name: string;
  className: string;
  level: number | null;
  hpCurrent: number | null;
  hpMax: number | null;
  ac: number | null;
  abilities: { key: string; score: number }[];
  conditions: string[];
  inventory: InventoryGroup[];
}

const ABILITY_ORDER = ["STR", "DEX", "CON", "INT", "WIS", "CHA"] as const;
const ABILITY_ALIASES: Record<string, (typeof ABILITY_ORDER)[number]> = {
  str: "STR",
  strength: "STR",
  dex: "DEX",
  dexterity: "DEX",
  con: "CON",
  constitution: "CON",
  int: "INT",
  intelligence: "INT",
  wis: "WIS",
  wisdom: "WIS",
  cha: "CHA",
  charisma: "CHA",
};

export function abilityModifier(score: number): number {
  return Math.floor((score - 10) / 2);
}

export function formatModifier(mod: number): string {
  return mod >= 0 ? `+${mod}` : `${mod}`;
}

function num(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() && Number.isFinite(Number(v))) return Number(v);
  return null;
}

function itemLabel(v: unknown): string | null {
  if (v == null || v === "") return null;
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return String(v);
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    const name = typeof o.name === "string" ? o.name : null;
    if (name) {
      const qty = num(o.quantity ?? o.qty ?? o.count);
      return qty != null && qty !== 1 ? `${name} ×${qty}` : name;
    }
    return JSON.stringify(v);
  }
  return String(v);
}

function humanize(key: string): string {
  const s = key.replace(/[_-]+/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function parseStats(stats: Record<string, unknown> | null | undefined): {
  abilities: SheetCharacter["abilities"];
  conditions: string[];
  ac: number | null;
} {
  const s = stats ?? {};
  const scores = new Map<string, number>();
  let ac: number | null = null;
  let conditions: string[] = [];
  for (const [k, v] of Object.entries(s)) {
    const lk = k.toLowerCase();
    const ability = ABILITY_ALIASES[lk];
    if (ability) {
      const n = num(v);
      if (n != null) scores.set(ability, n);
    } else if (lk === "ac" || lk === "armor_class" || lk === "armour_class") {
      ac = num(v);
    } else if (lk === "conditions" && Array.isArray(v)) {
      conditions = v.map(itemLabel).filter((x): x is string => !!x);
    }
  }
  return {
    abilities: ABILITY_ORDER.filter((k) => scores.has(k)).map((k) => ({
      key: k,
      score: scores.get(k) as number,
    })),
    conditions,
    ac,
  };
}

export function parseInventory(inv: Record<string, unknown> | null | undefined): InventoryGroup[] {
  const groups: InventoryGroup[] = [];
  for (const [k, v] of Object.entries(inv ?? {})) {
    const items = (Array.isArray(v) ? v : [v])
      .map(itemLabel)
      .filter((x): x is string => !!x);
    if (items.length) groups.push({ label: k === "items" ? "Items" : humanize(k), items });
  }
  return groups;
}

export function sheetFromLibrary(
  c: CharacterRead,
  assignment?: CharacterAssignmentHistoryRead | null,
): SheetCharacter {
  const stats = parseStats(assignment?.stats ?? c.base_stats);
  return {
    id: c.id,
    name: c.name,
    className: c.character_class,
    level: c.level,
    hpCurrent: assignment?.hp_current ?? c.hp_max,
    hpMax: assignment?.hp_max ?? c.hp_max,
    ac: stats.ac,
    abilities: stats.abilities,
    conditions: stats.conditions,
    inventory: parseInventory(assignment?.inventory ?? c.base_inventory),
  };
}

/** Merge the `done` payload's character into the current sheet (fields are optional). */
export function mergeTurnCharacter(
  prev: SheetCharacter | null,
  c: TurnCharacter,
): SheetCharacter {
  const base: SheetCharacter = prev ?? {
    id: null,
    name: "",
    className: "",
    level: null,
    hpCurrent: null,
    hpMax: null,
    ac: null,
    abilities: [],
    conditions: [],
    inventory: [],
  };
  const next: SheetCharacter = { ...base };
  if (typeof c.id === "number") next.id = c.id;
  if (typeof c.name === "string" && c.name) next.name = c.name;
  const cls = c.class ?? c.character_class;
  if (typeof cls === "string" && cls) next.className = cls;
  if (typeof c.level === "number") next.level = c.level;
  if (typeof c.hp_current === "number") next.hpCurrent = c.hp_current;
  if (typeof c.hp_max === "number") next.hpMax = c.hp_max;
  if (c.stats && typeof c.stats === "object") {
    const stats = parseStats(c.stats);
    if (stats.abilities.length) next.abilities = stats.abilities;
    next.conditions = stats.conditions;
    if (stats.ac != null) next.ac = stats.ac;
  }
  if (c.inventory && typeof c.inventory === "object") next.inventory = parseInventory(c.inventory);
  return next;
}

export interface HudCombatant {
  key: string;
  name: string;
  isPlayer: boolean;
  hp: number | null;
  hpMax: number | null;
  ac: number | null;
  init: number | null;
  alive: boolean;
  isCurrent: boolean;
}

/**
 * Combatants in initiative order (falls back to roster order), each flagged with whether it
 * is their turn. Works with both the current `combatants`(+`initiative_order`) shape and the
 * richer `initiative: [{name, init, ...}]` shape.
 */
export function orderCombatants(combat: CombatState): HudCombatant[] {
  const roster = combat.combatants ?? [];
  const used = new Set<number>();
  const findIdx = (name: string, isPlayer?: boolean): number => {
    const n = name.toLowerCase();
    let i = roster.findIndex(
      (c, idx) =>
        !used.has(idx) &&
        (c.name?.toLowerCase() === n || c.display_name?.toLowerCase() === n),
    );
    if (i < 0 && (isPlayer || n === "player")) i = roster.findIndex((c, idx) => !used.has(idx) && c.is_player);
    return i;
  };

  const out: HudCombatant[] = [];
  const fromRoster = (idx: number, init: number | null): HudCombatant => {
    used.add(idx);
    const c = roster[idx];
    return {
      key: `${idx}:${c.name}`,
      name: c.display_name || c.name,
      isPlayer: !!c.is_player,
      hp: num(c.hp_current),
      hpMax: num(c.hp_max),
      ac: num(c.ac),
      init,
      alive: c.alive !== false && (num(c.hp_current) ?? 1) > 0,
      isCurrent: false,
    };
  };

  if (Array.isArray(combat.initiative) && combat.initiative.length) {
    combat.initiative.forEach((e, i) => {
      const idx = findIdx(e.name, e.is_player);
      if (idx >= 0) {
        const base = fromRoster(idx, num(e.init));
        const hp = num(e.hp) ?? base.hp;
        out.push({
          ...base,
          hp,
          hpMax: num(e.hp_max) ?? base.hpMax,
          ac: num(e.ac) ?? base.ac,
          alive: typeof e.alive === "boolean" ? e.alive : base.alive && (hp == null || hp > 0),
        });
      } else {
        const hp = num(e.hp);
        out.push({
          key: `i${i}:${e.name}`,
          name: e.name,
          isPlayer: !!e.is_player,
          hp,
          hpMax: num(e.hp_max),
          ac: num(e.ac),
          init: num(e.init),
          alive: typeof e.alive === "boolean" ? e.alive : hp == null || hp > 0,
          isCurrent: false,
        });
      }
    });
  } else if (Array.isArray(combat.initiative_order) && combat.initiative_order.length) {
    for (const name of combat.initiative_order) {
      const idx = findIdx(name);
      if (idx >= 0) out.push(fromRoster(idx, null));
    }
  }
  roster.forEach((_, idx) => {
    if (!used.has(idx)) out.push(fromRoster(idx, null));
  });

  const cur = combat.current_turn;
  if (typeof cur === "string" && cur) {
    const n = cur.toLowerCase();
    const hit = out.find((c) => c.name.toLowerCase() === n) ?? (n === "player" ? out.find((c) => c.isPlayer) : undefined);
    if (hit) hit.isCurrent = true;
  } else if (typeof combat.current_turn_index === "number") {
    const hit = out[combat.current_turn_index];
    if (hit) hit.isCurrent = true;
  }
  return out;
}
