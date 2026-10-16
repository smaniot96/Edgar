# Rules verification against the 2014 rulebooks (EDG-149)

Scope: every code-owned rules table and rules text in the engine that was written from memory,
checked against the 2014 Player's Handbook (PHB), Dungeon Master's Guide (DMG) and Monster Manual
(MM) PDFs on the maintainer's machine. Those PDFs are gitignored and are not in this repository, so
this file cites them by PDF page number (`PDF p.N`, the page index in the PDF viewer, not the printed
folio). Stat blocks were read from the page images and transcribed to a scratch file outside the
repo. Selected source-shaped sentences also appear in deterministic parser fixtures; the full
books and extracted records are not committed.

No 2024 book is available locally. 2024 numbers are not implemented (see "Unverifiable" below).

## Verdict counts

| Verdict | Rows | Meaning |
|---|---|---|
| MATCH | 53 | Engine value equals the book value at the cited page |
| FIXED | 23 | Discrepancy found and corrected (17 in EDG-149, 6 in EDG-152); a regression test pins the book value and fails on the old code |
| PARTIAL | 20 | Documented subset or known gap; the contract text states it |
| NOT-IN-BOOKS | 1 section | The encounter budget (section 6). Artificer data is also NOT-IN-BOOKS and is noted in the class rows |

The rows are the five verdict tables below (96 in total); parser examples and summary tables are
excluded. A row may cover several values. EDG-152 turned two
PARTIAL rows (short-rest Hit Dice, prone) into `FIXED (EDG-152)`, split the Rage PARTIAL row into
two fixed rows and a smaller PARTIAL row, and added rows for Versatile weapons and armor Stealth
disadvantage. Later EDG-043 slices add save actions, legendary options, selected conditions and their
explicit remaining limits. The PARTIAL rows say which clauses remain outside the supported subset.
Counts are a direct recount of current verdict cells, not a claim that every rulebook clause is
implemented.

## 1. Resolution and character sheet (`apps/agent/src/agent/resolution.py`, `graph_combat.py`)

| Item | Code location | Book, PDF page | Verdict | Note |
|---|---|---|---|---|
| Ability modifier floor((score-10)/2) | `resolution.ability_mod` | PHB p.174, modifier table | MATCH | Pinned for scores 1-30 in `tests/agent/test_rules_book_values.py`. |
| Proficiency bonus by level | `resolution.proficiency_bonus` | PHB p.48 (Barbarian table column) | MATCH | +2 at 1-4, +3 at 5-8, +4 at 9-12, +5 at 13-16, +6 at 17-20. |
| DC tiers 10/15/20/25 | `resolution.DIFFICULTY_DC` | PHB p.175; DMG p.238 (Typical DCs: 5/10/15/20/25/30) | MATCH | Engine offers easy, medium (Moderate), hard and very hard. Very easy 5 and nearly impossible 30 are not tiers; the contract text now says so. |
| Saving-throw proficiencies per class | `resolution.CLASS_SAVES` | PHB p.46 (class table) | MATCH | All 12 PHB classes. Artificer is NOT-IN-BOOKS. |
| Default skill picks per class | `resolution.CLASS_SKILLS` | PHB class "Skills: Choose ..." lines, PDF pp.48, 53, 58, 67, 73, 85, 96, 108, 114 | MATCH | Every default is inside the class's option list. |
| Skill-to-ability map | `resolution.SKILL_ABILITY` | PHB pp.175-176 (skills by ability) | MATCH | |
| Class weapon proficiencies | `resolution.CLASS_WEAPONS` | PHB p.46; class sections PDF pp.66, 53, 78, 96, 101, 108, 114 | MATCH | Artificer is NOT-IN-BOOKS. |
| Spellcasting ability per class | `resolution.CASTING_ABILITY` | PHB class sections, PDF pp.54, 59, 67, 85, 93, 102, 114 | MATCH | Artificer is NOT-IN-BOOKS. |
| Hit die per class | `resolution.HIT_DIE` | PHB p.46 (class table, "Hit Point Die") | MATCH | Artificer is NOT-IN-BOOKS. |
| Weapon table: name, dice, finesse, ranged, simple | `resolution._WEAPONS` | PHB Weapons table, PDF p.150 | FIXED | Greatclub (simple melee, 1d8) was missing, and "club" matched it as a substring, giving 1d4. Blowgun (martial ranged, 1 piercing) was missing. Net (no damage) is not modelled. Other entries MATCH. |
| Versatile weapons: two-handed damage | `resolution._WEAPONS`, `resolution.roll_attack_damage` | PHB Versatile, PDF p.148; Weapons table, PDF p.150 | FIXED (EDG-152) | The table had no Versatile property, so a two-handed grip never changed the die. Quarterstaff, spear, trident (1d8) and battleaxe, longsword, warhammer (1d10) now deal it when the player declares both hands and the sheet lists no shield. Pinned in `tests/agent/test_rules_book_values.py`. |
| Finesse and ranged flags | `resolution._WEAPONS` | PHB p.148 (finesse); p.150 | MATCH | |
| Light property: club, dagger, handaxe, light hammer, sickle, scimitar, shortsword; hand crossbow (ranged) | `resolution._WEAPONS` (`Weapon.light`) | PHB Weapons table, PDF p.150 | MATCH | Pinned in `tests/agent/test_rules_book_values.py`. |
| Two-weapon fighting: a bonus-action attack with a different light melee weapon, adding the ability modifier to damage only when negative | `resolution.off_hand_weapon`, `resolution.roll_attack_damage`, `graph_combat` | PHB Two-Weapon Fighting, PDF p.196 | PARTIAL | The attack, its bonus-action slot and its damage match, and Rage's flat bonus and Sneak Attack's once per turn still apply. The sheet lists carried weapons, not what each hand holds, so a listed shield blocks the attack and the main hand is the weapon the engine picks for the Attack action. Throwing either weapon and the Two-Weapon Fighting style (modifier added) are not modelled. Pinned in `tests/agent/test_two_weapon_fighting.py`. |
| Attack ability: STR melee, DEX ranged, finesse higher of the two | `resolution._attack_ability` | PHB p.195 ("Ability Modifier"); p.148 (finesse) | MATCH | Thrown melee weapons (javelin, handaxe) use STR, as the book's thrown exception implies. |
| Natural 20 hits and is critical; natural 1 misses | `resolution.resolve_check` | PHB p.195 ("Rolling 1 or 20") | MATCH | |
| Critical hit: roll all damage dice twice, add modifiers once | `resolution._double_dice` | PHB p.197 ("Critical Hits") | MATCH | Rage's flat bonus is not doubled; consistent with the book. |
| Advantage/disadvantage; both together cancel | `resolution.combine_roll_modes` | PHB p.174 (Advantage and Disadvantage) | MATCH | |
| Initiative = DEX modifier | `resolution.initiative_bonus` | PHB p.190 ("Combat step: initiative", a DEX check) | MATCH | Bard's Jack of All Trades also applies to initiative in the book; that class feature is outside the supported set. |
| Armor table: padded, leather, studded, chain shirt, breastplate, half plate, ring mail, chain mail, splint, plate | `graph_combat._player_ac` | PHB Armor table, PDF p.146 | MATCH | Medium armor DEX cap +2 (PDF p.145). Stealth disadvantage: see the next row. Heavy armor STR requirement (PARTIAL): its only effect is -10 ft of speed (PDF p.145), and speed is not simulated. |
| Armor table: Stealth disadvantage (padded, scale mail, half plate, ring mail, chain mail, splint, plate) | `resolution.armor_hinders_stealth`, `resolution.resolve_check` | PHB Armor table, PDF p.146; rule text PDF p.145 | FIXED (EDG-152) | Armor never affected Stealth checks. A Stealth check made in those armors now has disadvantage (a `RollModeSource`, so engine advantage cancels it). Pinned in `tests/agent/test_rules_book_values.py`. |
| Armor table: hide armor (12 + DEX, max 2) | `graph_combat._player_ac` | PHB p.146 | FIXED | Hide was not in the table, so it fell through to unarmored (10 + DEX). |
| Shield +2 | `graph_combat._player_ac` | PHB p.146 (Shield row, +2) | MATCH | |
| Unarmored Defense, barbarian: 10 + DEX + CON, shield allowed | `graph_combat._player_ac` | PHB p.49 | FIXED | Was 10 + DEX for every class. |
| Unarmored Defense, monk: 10 + DEX + WIS, no shield | `graph_combat._player_ac` | PHB p.79 | FIXED | A monk with a shield gets no benefit (10 + DEX + shield). |
| Potion of healing 2d4+2, greater 4d4+4, superior 8d4+8, supreme 10d4+20 | `resolution._HEALING_ITEMS` | DMG "Potions of Healing" table, PDF p.188 | MATCH | |
| Goodberry: 1 HP per berry | `resolution._HEALING_ITEMS` | PHB goodberry text, PDF p.247 | MATCH | Modelled as 1d1. |
| "Healing draught", "healing elixir" = 2d4+2 | `resolution._HEALING_ITEMS` | No such items in the DMG or PHB (DMG PDF p.168 has an Elixir of Health with no HP) | FIXED | Mapping removed: such items now get no engine healing. |
| Short rest: spend a Hit Die, regain the roll plus CON; no minimum | `resolution.heal_from_rest` | PHB p.186 (PDF p.187), "Short Rest" | FIXED | Floor of 1 HP removed (a roll below zero regains nothing; the die is still spent). |
| Short rest: one or more Hit Dice may be spent | `resolution.heal_from_rest`, `resolution.declared_hit_dice` | PHB PDF p.187 | FIXED (EDG-152) | A short rest spent exactly one die. The player's declared count (digits or words up to ten) is now rolled as one `NdX+N*CON` expression, capped at the dice left and defaulting to one. The book's choice to spend another die after each roll is not offered. Pinned in `tests/agent/test_hit_dice.py`. |
| Long rest: all HP; spell slots; Second Wind and Action Surge | `resolution.heal_from_rest`, `class_features.class_feature_after_rest` | PHB PDF p.187; Fighter p.72-73; Barbarian p.49 | MATCH | The book's "at least 1 HP at start" and "once per 24 hours" are not checked (PARTIAL). |
| Long rest: spent Hit Dice recovered = half the total, rounded down | `resolution.hit_dice_recovered_by_long_rest` | PHB PDF p.187 ("up to ... half of the character's total number") | FIXED | The old code had a minimum of one, so a level-1 character got its die back. The book has no minimum: level 1 recovers 0. |
| Second Wind: bonus action, 1d10 + fighter level, once per short or long rest | `resolution.spend_second_wind` | PHB PDF p.73 | MATCH | 2024 Fighter table not implemented (unverifiable locally). |

## 2. Class features (`apps/agent/src/agent/class_features.py`)

| Item | Code location | Book, PDF page | Verdict | Note |
|---|---|---|---|---|
| Rage uses per long rest: 2, 3, 4, 5, 6 at levels 1, 3, 6, 12, 17 | `class_features._RAGE_USES` | PHB Barbarian table, PDF p.48 | MATCH | Pinned by level in `test_rules_book_values.py`. |
| Rage damage +2 / +3 (level 9) / +4 (level 16) on Strength melee hits | `class_features.rage_damage_bonus` | PHB Barbarian table, PDF p.48 | MATCH | |
| Rage restored by a long rest only | `class_features.class_feature_after_rest` | PHB PDF p.49 ("finish a long rest") | MATCH | |
| Rage: resistance to bludgeoning, piercing and slashing damage; benefits only outside heavy armor | `class_features.rage_resists`, `class_features.rage_damage_bonus`, `graph_combat` | PHB PDF p.49 | FIXED (EDG-152) | Resistance was not applied, and the damage bonus ignored heavy armor. Foe damage of those types is now halved (rounded down) while raging, outside heavy armor. An attack with no stated damage type is not halved. Pinned in `tests/agent/test_class_features.py`. |
| Rage lasts 1 minute | `class_features.RAGE_ROUNDS`, `graph_combat` | PHB PDF p.49 | FIXED (EDG-152) | A rage lasted until the encounter ended. It now ends at the start of the hero's turn 10 rounds after it began. |
| Rage ends early when unconscious or a turn passes with no attack or damage; Strength advantage; no spells | not modelled | PHB PDF p.49 | PARTIAL | Unconsciousness ends the encounter in the engine. The no-attack-or-damage rule needs per-turn attack and damage tracking, and Strength advantage would touch only the maneuver contest. Both are stated in the contract. |
| Action Surge: one use from level 2, two from 17 | `class_features.max_uses` | PHB Fighter table, PDF p.72 | MATCH | |
| Action Surge effect: one additional action | `class_features.spend_use` | PHB PDF pp.72-73 | PARTIAL | Engine adds one extra weapon attack only. Stated in the contract. Left in EDG-152: another action (Dash, a spell, an item) would run the combat round's single action chain twice, which is a restructuring of the round rather than a rule slice. |
| Sneak Attack dice: 1d6 per two levels, rounded up | `class_features.sneak_attack_roll` | PHB Rogue table, PDF p.96; text PDF p.97 | MATCH | Pinned at ten levels from 1 to 20 in `test_rules_book_values.py`. |
| Sneak Attack trigger: advantage, or an ally within 5 ft of the target | `class_features.sneak_attack_roll` | PHB PDF p.97 | PARTIAL | Only engine advantage triggers it; the ally clause is not tracked. Stated in the contract. Left in EDG-152: the hero fights alone, so no ally combatant exists whose position could satisfy the clause. |
| Sneak Attack: finesse or ranged weapon, once per turn, criticals double | `class_features.sneak_attack_roll` | PHB PDF p.97 | MATCH | |

## 3. Spells (`apps/agent/src/agent/spells.py`)

| Item | Code location | Book, PDF page | Verdict | Note |
|---|---|---|---|---|
| Fire Bolt: cantrip, 120 ft, spell attack, 1d10 fire, +1d10 at 5/11/17 | `spells.CATALOG` | PHB PDF p.243 | MATCH | Classes sorcerer, wizard (PDF pp.210-211). |
| Magic Missile: 1st, 120 ft, three automatic darts of 1d4+1 force, +1 dart per slot | `spells.CATALOG` | PHB PDF p.258 | MATCH | Classes sorcerer, wizard (PDF pp.210-211). |
| Sacred Flame: cantrip, 60 ft, DEX save, 1d8 radiant, scales 5/11/17 | `spells.CATALOG` | PHB PDF p.273 | MATCH | Class cleric (PDF p.208). |
| Cure Wounds: 1st, touch, 1d8 + mod, +1d8 per slot | `spells.CATALOG` | PHB PDF p.231 | PARTIAL | Dice and upcast MATCH. The engine heals only the caster; a touched ally is not modelled. Left in EDG-152: an ally has no engine hit points to heal (an NPC row carries only a free-form stat block). |
| Healing Word: 1st, bonus action, 60 ft, 1d4 + mod, +1d4 per slot | `spells.CATALOG` | PHB PDF p.251 | PARTIAL | Dice, action and upcast MATCH. Caster-only target, as above (left in EDG-152 for the same reason). |
| Guiding Bolt: 1st, 120 ft, spell attack, 4d6 radiant; next attack against target has advantage until end of your next turn; +1d6 per slot | `spells.CATALOG`, `conditions` | PHB PDF p.249 | MATCH | Mark kept for two rounds, which is "until the end of your next turn". Not concentration. Class cleric (PDF p.208). |
| Blindness/Deafness: 2nd, 30 ft, CON save, one minute, repeat save at end of each turn, not concentration | `spells.CATALOG`, `conditions` | PHB PDF p.220 | MATCH | Duration 1 minute = 10 rounds. Blindness only, one target: PARTIAL (documented). |
| Hold Person: 2nd, 60 ft, WIS save, paralyzed for up to one minute (concentration), humanoid only, repeat save at the end of each of its turns; one more humanoid per slot level above 2nd | `spells.CATALOG`, `spells.target_count`, `conditions`, `graph_combat` | PHB PDF p.252 | PARTIAL | Save, duration, humanoid gate, paralysis, repeat saves and the extra targets MATCH; each target rolls its own save and the spell lasts while any one is held. The extra targets are the foes the player names (the parser's target list), then the next humanoids in roster order, and fewer when fewer humanoids stand. The 60-foot range and the 30-foot limit between targets are not modelled: the engine tracks melee and ranged bands, not distances. Pinned in `tests/agent/test_concentration.py`. |
| Concentration: one spell at a time; ends when another concentration spell is cast, when a damage save fails (DC 10 or half the damage, one save per source), and when the caster is incapacitated or dies | `conditions.concentration_save`, `drop_concentration_if_incapacitated`, `drop_concentration`, `settle_concentration`, `graph_combat` | PHB p.203, PDF p.204 | MATCH | For supported catalog spells and conditions: a fall to 0 HP ends concentration without a save, and stunned or paralyzed ends it immediately, including persisted effects on load and a stun applied after a successful damage concentration save. Other incapacitating conditions remain outside the supported subset. Only Hold Person in the current catalog needs concentration; the other catalog spells are instantaneous or explicitly not concentration. Pinned in `tests/agent/test_concentration.py` and `tests/agent/test_monster_conditions.py`. |
| Full caster slot table (wizard, sorcerer, bard, cleric, druid) | `spells.FULL_CASTER_SLOTS` | PHB Wizard table, PDF p.114 | MATCH | All 20 rows read; rows 1, 5, 9, 17, 20 pinned in tests. |
| Half caster slot table (paladin, ranger) | `spells.HALF_CASTER_SLOTS` | PHB Paladin table, PDF p.84; Ranger table PDF p.91 | MATCH | Paladin: all 20 rows. Ranger: rows 2-9 read, same progression. |
| Cantrip dice scale at levels 5, 11, 17 | `spells._cantrip_tier` | PDF pp.243, 273 | MATCH | |
| Spell save DC = 8 + proficiency + casting modifier; spell attack = proficiency + modifier | `spells.spell_save_dc`, `resolution.check_spec` | PHB PDF p.54 (spell text) | MATCH | |
| Lowest slot when none is named | `spells.plan_cast` | PHB PDF p.202 ("Casting a spell at a higher level") | MATCH | The book lets the caster choose any slot of the spell's level or higher; lowest is a stated default. |
| Saving throw for a restrained target, DEX saves at disadvantage | `spells.resolve_spell`, `graph_combat` | PHB Appendix A "Restrained", PDF p.293 | FIXED | A restrained foe's Sacred Flame save was rolled normally. Now at disadvantage, with both faces logged. |

## 4. Monster stat blocks (`apps/agent/src/agent/monster_stats.py`)

| Item | Code location | Book, PDF page | Verdict | Note |
|---|---|---|---|---|
| Weapon damage types per weapon | `monster_stats.WEAPON_DAMAGE_TYPE` | PHB Weapons table, PDF p.150 | FIXED | Greatclub (bludgeoning) and blowgun (piercing) added. All other types MATCH. |
| Troll Regeneration: 10 HP at start of turn; suppressed by acid or fire taken since its last turn | `monster_stats._regeneration`, `regenerate` | MM Troll, PDF p.292 | MATCH | Parsed amount 10 and suppressors acid and fire. |
| Troll: dies only if it starts a turn at 0 HP and does not regenerate | `monster_stats.parse_monster_stats` | MM PDF p.292 | PARTIAL | Engine defeats a creature at 0 HP at once. The clause is declared in the combat start message, not executed. Changing defeat timing touches the round loop, so it is left open. Left in EDG-152: a creature at 0 HP that is not yet dead needs a new "down but not dead" state read by targeting, the victory check, nonlethal knockout and captive dispositions, the narrator roster and the HUD, and a hero must still be able to target it (fire or acid is the book's counter). That is a state-model change, not a rule slice. |
| Multiattack "makes two attacks with its X" | `monster_stats._multiattack` | MM Goblin Boss, PDF p.167 | FIXED | Counts were unreadable, so one attack was used. |
| Attack "Melee or Ranged Weapon Attack" | `monster_stats._ATTACK` | MM Goblin Boss javelin, PDF p.167 | FIXED | Was declared as a trait, never rolled. |
| Recharge names with an en dash, "(Recharge 5–6)" | `monster_stats._ENTRY` | MM Giant Spider, PDF p.329; Young Red Dragon, PDF p.99 | FIXED | The entry failed the name pattern, so its text was appended to the previous attack and the recharge was never declared. |
| Resistance "from non magical attacks" (split word in the text layer) | `monster_stats._damage_rules` | MM, on about 50 pages; text layer splits "non magical" | FIXED | Read as a magic qualifier, so the resistance was dropped. |
| "that aren't silvered" / "that aren't adamantine" exception | `monster_stats._damage_rules` | MM PDF pp.71, 141, 301 | FIXED | Exception is now declared; the hero's weapons count as plain, so the resistance applies. |
| Ability modifier "(- 1)" with a space | `monster_stats._abilities` | MM text layer (Goblin, Wolf, Bandit blocks) | FIXED | Lost the modifier before. |
| Multiattack "second attack has disadvantage" | `monster_stats.parse_monster_stats` | MM Goblin Boss, PDF p.167 | FIXED | Was silently ignored; now declared. |
| Resistances, immunities, vulnerabilities by type | `monster_stats._damage_rules` | MM Young Red Dragon (fire immunity), PDF p.99 | MATCH | |
| Speed, senses, saves, skills | `monster_stats.MonsterStats` | MM PDF pp.99, 167, 292, 329, 342, 344 | MATCH | Speed is declared, movement not simulated. |
| Monster spellcasting: exact listed catalog spells, slots, casting numbers, concentration and supported effects | `monster_stats._spellcasting_entry`, `graph_combat`, `spells.resolve_monster_spell` | MM Mage, PDF p.348; PHB spell descriptions, PDF pp.220, 231, 243, 249, 251, 252, 258, 273; bonus-action casting PDF p.203 | PARTIAL | Only exact catalog names listed at their catalog level execute, using record-owned ability, caster level, DC and attack bonus. Available slots of that level are spent; cantrips are free and scale by caster level. Healing is self-only, attacks/saves target the hero, and no monster upcasting or innate casting is simulated. A bonus spell may accompany an action cantrip or weapon action, never a leveled action spell. Marks, blindness, Hold Person, source-owned concentration, incapacitation and repeat saves persist across reload. Unknown list entries stay unexecuted and public declarations give a count without revealing names. Exact ranges and ally targets remain outside the bands. Pinned in `tests/agent/test_monster_spellcasting.py` and the independent completion review. |
| Breath weapon: Recharge 5-6, DC, saving-throw ability, damage dice and type, half damage on a success | `monster_stats._save_action`, `graph_combat` (`enemy_save_action`) | MM Young Red Dragon Fire Breath, PDF p.99 (30-foot cone, DC 17 Dexterity, 56 (16d6) fire); Red Dragon Wyrmling, PDF p.99 | MATCH | Added in EDG-043. The hero's save is d20 + ability modifier + proficiency when the class is proficient (`resolution.CLASS_SAVES`, PHB p.46); a success takes half, rounded down. The wording is read exactly, so a clause such as "provided that it has no creature grappled" (Ankheg Acid Spray) or a rider after the damage stays declared. The cone or line shape and range are not modelled: the hero is the only creature in the area. Pinned in `tests/agent/test_monster_save_actions.py`. |
| Legendary actions: N per round, at most one option at the end of another creature's turn, regained at its own turn start, none while incapacitated | `monster_stats.legendary_plan`, `graph_combat` | MM Legendary Actions, PDF p.12; Ancient Black Dragon, PDF p.88 | PARTIAL | The record owns the budget and option costs; the engine chooses the most expensive affordable option at each represented other-creature turn end, subject to band, capacity and hero survival. Spent points persist and reset at its own turn start. An authored surprise flag blocks use through its first turn. Installed MM p.12 establishes timing/budget/incapacity; it is not cited as containing an explicit surprise sentence. Only the named supported options execute. Pinned in `tests/agent/test_monster_legendary.py`. |
| Authored encounter surprise | `Entity.surprise`, `graph_combat` | PHB Surprise, PDF p.190 | PARTIAL | Reviewed actor setup names actor or hero; it is never inferred from prose. The affected creature cannot act/move on its first turn or react until that turn ends; legendary/lair use also waits. The saved flag clears at that turn end. Perception/Stealth contests and group surprise are not simulated. Pinned in `tests/agent/test_monster_legendary.py`. |
| Legendary option naming one of the creature's own attacks: "makes a tail attack" (Costs 1) | `monster_stats._legendary_option` | MM Ancient Black Dragon Tail Attack, PDF p.88; Sphinx Claw Attack, PDF p.282; Unicorn Hooves, PDF p.295 | MATCH | The option is rolled as the creature's own listed attack (attack roll, damage, damage type), only when exactly one plain attack of that name exists. "makes one attack" (Empyrean), "or uses its ..." (Mummy Lord, Tarrasque, Kraken) and an attack that recharges or is a save action stay declared. |
| Wing Attack (Costs 2 Actions): Dexterity save or damage and prone, then flight | `monster_stats._legendary_option`, `graph_combat` | MM Ancient Black Dragon, PDF p.88; adult/ancient dragon wording | PARTIAL | Damage and prone execute on a failed save, none on success; flight changes the foe to the ranged band and can prevent a later melee option. A melee-only foe may close again on its own turn. The 10/15-foot radius is approximated by the melee band and exact half flying speed is not tracked. Pinned in `tests/agent/test_monster_legendary.py`. |
| Other legendary options: Detect, Teleport, Move, Eye Ray, Searing Burst, Blinding Gaze, Energy Drain, Frightening Gaze, spellcasting and condition options | `monster_stats.parse_monster_stats` | MM Adult dragons (Detect), PDF p.88; Solar, PDF p.19; Demilich, PDF p.49; Lich, PDF p.203; Mummy Lord, PDF p.230; Sphinx, PDF p.282 | PARTIAL | Declared by name ("Detect: legendary action not executed"), never guessed: they need ability checks, movement, spells, two damage types, hit-point maximum changes or conditions the engine cannot read exactly. |
| Lair actions: initiative count 20 losing ties, one option, no consecutive repeat | `Entity.lair_reference`, `resolve_creature`, `monster_stats.parse_lair_actions`, `graph_combat` | MM Lair Actions, PDF p.12; red dragon, PDF p.100; unsupported black dragon options PDF p.90 | PARTIAL | Explicit reviewed actor binding to an unlocked private planner passage is required; ordinary flags cannot grant it. Full red-dragon magma and tremor options execute: DEX 15, 6d6 fire/half on success, or prone on failure. The event follows all tied creatures across the full roster; the last action/round persist to prevent consecutive repetition. Owner incapacitation/surprise blocks use. The hero is the target; points, grounded terrain, exact radii, persistent volcanic gas, insects, darkness, pull and dismissal remain unsupported. Extra clauses cannot authorize an executable substring. Pinned in `tests/agent/test_monster_completion_review.py`. |
| Frightful Presence: Wisdom save, frightened for one minute, repeat save, immunity after a success or ending the effect | `monster_stats._save_action`, `_multiattack`, `conditions`, `graph_combat` | MM adult dragon, PDF pp.98-99 | PARTIAL | Exact wording only; opens the Multiattack when named. Immunity is tracked only for this encounter, not across the full 24 hours. Sight, range and independent simultaneous fear sources retain the limits stated above. Pinned in `tests/agent/test_monster_conditions.py`. |
| Mind Blast: Intelligence save or psychic damage and stunned for one minute, repeat save, Recharge 5-6 | `monster_stats._save_action`, `conditions`, `graph_combat` | MM mind flayer, PDF p.223 | MATCH | Exact supported wording only; the hero is the sole target. Parsed damage dice, DC, ability, duration and recharge stay engine-owned. Area shape/range are outside the simulated bands. Pinned in `tests/agent/test_monster_conditions.py`. |
| Hit riders: frightened or stunned until the end of the attacker's next turn, without a repeat save | `monster_stats._attack`, `conditions.rider_save`, `end_of_source_turn`, `graph_combat` | MM Scarecrow Claw, PDF p.269; Empyrean Maul, PDF p.131 | MATCH | Exact supported Wisdom/Constitution wording only. Stable source identity controls the turn-end timer; repeat failed saves renew a same-source duration rather than stacking penalties. Pinned in `tests/agent/test_monster_conditions.py`. |

### Parser results on real Monster Manual stat blocks

The seven entries were read from the page images (Young Red Dragon PDF p.99; Goblin and Goblin Boss
PDF p.167; Troll PDF p.292; Giant Spider PDF p.329; Wolf PDF p.342; Bandit PDF p.344). The text
layer interleaves the two columns, so each block was transcribed by hand to a scratch file.

| Creature | Before EDG-149 | After EDG-149 |
|---|---|---|
| Troll | AC 15, HP 84, abilities, Multiattack (1 bite, 2 claws), Regeneration 10 with acid/fire suppression, both attacks read. 0 HP clause declared. | Unchanged. |
| Goblin | AC 15, HP 7, both attacks read. | Unchanged. |
| Goblin Boss | Javelin not read (declared as a trait). Multiattack counts unreadable, so one attack. | Scimitar x2 from Multiattack, javelin read, second-attack disadvantage declared. |
| Wolf | Bite read, prone rider declared. | Unchanged. |
| Giant Spider | Bite read. The recharge web attack was glued onto the bite text and not declared. | Web (Recharge 5–6) declared. |
| Bandit | Scimitar and light crossbow read. | Unchanged. |
| Young Red Dragon | Multiattack, bite, claw, fire immunity, saves read. Fire Breath not declared. | Fire Breath (Recharge 5–6) declared (executed since EDG-043: DC 17 Dexterity, 56 (16d6) fire, half on a success). |

Synthetic, format-faithful fixtures (own numbers and wording, not copied) live in
`tests/agent/test_monster_stats.py` (Gutter Captain, Stone Sentinel) and cover each quirk above.

Known text-layer risk, not an engine defect: the Monster Manual's two-column pages come out of
`pdftotext` (layout or raw) with the columns interleaved, and OCR breaks words ("slash ing", "Perce
ption"). A creature record built from that text can mix two stat blocks; the existing single-record
check in `db/vector` rejects such mixes, and the parser declares what it cannot read.

## 5. Conditions and actions (`conditions.py`, `combatants.py`, `action_economy.py`)

| Item | Code location | Book, PDF page | Verdict | Note |
|---|---|---|---|---|
| Blinded: attacks by it at disadvantage; attacks against it at advantage | `combatants._OWN_ATTACK_DISADVANTAGE`, `_AGAINST_ADVANTAGE` | PHB Appendix A, PDF p.291 | MATCH | |
| Blinded: auto-fails sight-based ability checks | not modelled | PHB PDF p.291 | PARTIAL | Foes make no ability checks in the engine, so this is not reachable; not applied. |
| Restrained: speed 0; attacks against it at advantage; its attacks at disadvantage | `combatants` | PHB PDF p.293 | MATCH | Speed 0 is not simulated (movement not modelled). |
| Restrained: DEX saves at disadvantage | `graph_combat`, `spells` | PHB PDF p.293 | FIXED | See the spells table. |
| Prone: its attacks at disadvantage; attacks against it at advantage from within 5 ft, otherwise disadvantage | `combatants.attack_roll_mode` | PHB PDF p.293 | FIXED (EDG-152) | Ranged attacks always got disadvantage. The melee band (EDG-044) is the within-5-ft source, so a ranged attack from within 5 ft now has advantage, which the ranged-within-5-ft disadvantage (PHB p.195) cancels. Pinned in `tests/agent/test_combat_effects.py`. |
| Hero standing up from prone | `graph_combat` (_STAND_UP) | PHB Being Prone, PDF p.190 | PARTIAL | An explicit stand-up declaration clears the hero's prone effect without spending an action. The movement-band abstraction does not track speed, so the book's cost of half the hero's movement is not charged. Pinned in `tests/agent/test_positioning.py`. |
| Poisoned hero: attack rolls and ability checks at disadvantage | `combatants.attack_roll_mode`, `impaired_checks`, `resolution.resolve_check`, `graph_combat` | PHB Appendix A, PDF p.293 | MATCH | Applied only by the exact supported monster rider; end-of-turn repeat saves and the one-minute duration are engine-owned. Pinned in `tests/agent/test_monster_riders.py` and `tests/agent/test_monster_conditions.py`. |
| Frightened hero: attacks and ability checks at disadvantage while the source is in sight; no willing approach to the source | `combatants`, `conditions.fear_refusal`, `graph_combat` | PHB Appendix A, PDF p.291 | PARTIAL | Stable source IDs distinguish same-name foes. The engine approximates sight by a source that still stands, and ends movement restrictions when the source is down while retaining the condition, duration and repeat save. Exact line of sight and independent simultaneous fear sources are not modelled. A new fear applied ahead of the hero is checked again before movement/action execution. Pinned in `tests/agent/test_monster_conditions.py`. |
| Stunned hero: no action, movement or flight; automatic failure of Strength and Dexterity saves; attacks against it at advantage; concentration and Dodge benefits end | `combatants.can_act`, `save_auto_fails`, `attack_roll_mode`, `conditions`, `graph_combat` | PHB Appendix A, PDF p.293; concentration PDF p.204; Dodge PDF p.193 | MATCH | Only exact supported monster text applies the condition. An incapacitated turn spends no hero action, slot or item, and a short stun ending before the hero turn permits the available declaration. Pinned in `tests/agent/test_monster_conditions.py`. |
| Paralyzed and other conditions beyond the supported subset | `conditions`, `supported_rules`, `graph_combat` | PHB Appendix A, PDF pp.291-293 | PARTIAL | Catalog Hold Person paralyzes humanoid foes or the hero: no actions, movement, bonus actions or flight, automatic STR/DEX save failure and advantage against the target. Hero melee hits against a paralyzed foe critically hit; monster weapon/spell hits against a paralyzed hero critically hit from the melee band. A miss still misses. Repeat saves, duration and source-owned concentration control expiry. Other paralysis wording, charmed, grappled and other unsupported conditions remain narrative tags. Pinned in `tests/agent/test_monster_spellcasting.py` and the independent completion review. |
| Dodge: attacks against the hero have disadvantage until the next turn | `combatants.attack_roll_mode`, `graph_combat` | PHB p.193 ("Dodge"), PDF p.193 | PARTIAL | Engine applies the disadvantage only while the hero is not stunned or paralyzed. It does not check visibility or the loss of the benefit at speed 0 (speed is not simulated). Left in EDG-152: the only foe that cannot see the hero is a blinded one, whose own attack disadvantage already gives the same roll, so it does not change an outcome today. |
| Dodge: Dexterity saving throws with advantage | `combatants.save_roll_mode`, `graph_combat` (`enemy_save_action`) | PHB p.193 ("Dodge"), PDF p.193 | MATCH | Added in EDG-043. The hero's Dexterity save against a monster's save action rolls two d20s and keeps the higher while dodging; other saves are unchanged. Pinned in `tests/agent/test_monster_save_actions.py`. A stunned or paralyzed hero automatically fails a Dexterity save; Dodge provides no benefit. Speed 0 is not simulated. |
| One action and one bonus action per turn | `action_economy.refusal_for` | PHB "Bonus Actions", PDF p.190 | MATCH | |
| Advantage and disadvantage cancel | `resolution.combine_roll_modes` | PHB PDF p.174 | MATCH | |

## 6. Encounter budget (`apps/agent/src/agent/encounter_budget.py`)

NOT-IN-BOOKS. The DMG and PHB do not define a solo budget. Report only, nothing changed:

- The DMG XP thresholds (DMG PDF p.82) for one level-1 hero are 25 / 50 / 75 / 100 (easy to deadly).
- The level-1 budget allows a lone boss of up to 30 HP, +5 to hit and 5 average damage per hit. The
  MM's Goblin Boss is CR 1, 200 XP (PDF p.167), twice the level-1 deadly threshold, so the level-1
  cap can produce a deadly solo fight.
- At level 20 the budget allows four foes, each up to about 173 HP (boss) or 63 HP (others), up to 29
  average damage and +9 to hit. The DMG deadly threshold for one level-20 hero is 12,700 XP (PDF p.82).
  Several creatures of that strength are far above it, so the cap is permissive at the top too.
- Conclusion: the budget is hand-set and not tied to XP. It is sane as a ceiling for a single hero
  only at mid levels. Retuning it is a design decision, not a book fact, so it is left unchanged.

## Unverifiable locally (2024 and other editions)

No 2024 book is on this machine, so these 2024 claims are not implemented or verified:

- Fighter 2024 Second Wind uses (more uses at higher levels). The engine keeps the 2014 single use.
- 2024 Hit Dice recovery and short-rest rules, which the README and contract say "follow the same rule".
- 2024 Action Surge, Rage and Sneak Attack tables (the engine follows 2014).
- Any 2024 spell changes (the catalog follows 2014 descriptions).
- Artificer (class table, saves, casting ability, hit die): from Eberron and Tasha's material, not in these books.

## Historical EDG-149 tests and lint

- New regression cases: 107 in `tests/agent/test_rules_book_values.py` (new file) and 5 synthetic
  parser cases appended to `tests/agent/test_monster_stats.py`. Run against the pre-EDG-149 source,
  15 of the 112 fail, each for a discrepancy listed above; all 112 pass on this branch.
- Existing tests that pinned the discrepant values were updated with page citations:
  `tests/agent/test_hit_dice.py`, `tests/agent/test_supported_rules.py` (comment),
  `tests/test_hit_dice_persistence.py` (comment), `tests/api/test_hit_dice_reload.py`
  (DB-backed; it runs in hosted CI only and was not run locally).
- Full suite: 1002 passed, 169 skipped (DB-backed), 2 failed. The 2 failures are the known
  Docker-only Qdrant tests (`test_native_qdrant_exact_rules_filter_edition_and_staged_evidence`,
  `test_disposable_qdrant_http_lost_response_and_active_nested_filter`).
- `uv run ruff check .` passes. `ruff format --check` passes on the changed files, except
  `graph_combat.py`, which was already unformatted at HEAD and was left as it was.
- Frontend (one tooltip string changed): `npm ci`, `npx vitest run` (14 files, 87 tests), `npm run lint`
  and `npm run build` all pass.

## EDG-152: partial rows closed

- Fixed rows: prone attackers within 5 ft, Versatile two-handed damage, short-rest Hit Dice count,
  armor Stealth disadvantage, Rage resistance and heavy-armor limit, Rage 1-minute limit. Each has a
  page-cited regression test in `tests/agent/test_combat_effects.py`,
  `tests/agent/test_rules_book_values.py`, `tests/agent/test_hit_dice.py` or
  `tests/agent/test_class_features.py` (46 test cases added). Run against the pre-EDG-152 source,
  the new cases that can run there fail, and the two files that import new names fail to import.
- Rows left PARTIAL (each note says why): Action Surge as a full extra action, the Sneak Attack
  ally clause, Cure Wounds and Healing Word on an ally, Troll regeneration from 0 HP, Dodge
  visibility and Dexterity-save advantage, Rage's early end and Strength advantage, and the
  heavy-armor Strength requirement (speed is not simulated).
- Full suite: 1288 passed, 192 skipped (DB-backed), 2 failed (the same Docker-only Qdrant tests as
  above). `uv run ruff check .` passes.

## EDG-043: hero saving throws against save-based monster actions

- Added rows: the breath weapon of an exact record (section 4) and Dodge's advantage on Dexterity
  saving throws (section 5). The Dodge row for attacks stays PARTIAL for visibility and speed 0.
- Left open: area effects on more than the hero, legendary and lair actions, monster spellcasting,
  other conditions, and save actions whose wording is not the exact breath-weapon sentence.
- Tests: `tests/agent/test_monster_save_actions.py` (16 cases, scripted dice). The Gutter Captain
  fixture in `tests/agent/test_monster_stats.py` now reads its Spit Breath as an executed action.
- Full suite: 1431 passed, 200 skipped (DB-backed), 2 failed (the same Docker-only Qdrant tests as
  above). `uv run ruff check .` passes.

## EDG-043: legendary actions

- Added rows (section 4): the legendary budget and timing, an option naming the creature's own attack,
  the Wing Attack, the other options and lair actions. At each other creature's represented turn end,
  the engine chooses at most one affordable option, most expensive first (ties in record order).
  A budget of 3 permits Wing Attack (2) at one boundary and Tail Attack (1) at a distinct later
  creature's turn end; it never permits both at the same boundary. Budget resets at its own turn.
- Left open: the surprise clause, the Wing Attack's prone effect and flight, lair actions (they need
  an authored "in its lair" field), and the options listed as declared.
- Tests: `tests/agent/test_monster_legendary.py` (23 cases, scripted dice). Root's independent focused
  run including save actions, riders and the supported-scope contract passed 112 tests without skips.

## EDG-043: spell cases

- Added rows: Hold Person (section 3) and Concentration (section 3), both PARTIAL for the clauses the
  engine cannot express. Hold Person cast with a 3rd-level or higher slot holds one more humanoid per
  level above 2nd; no other catalog spell needs concentration, so none gained it.
- Left open: further concentration spells (for example Hold Monster, Bless or Hunter's Mark) are not in
  the catalog and were not added. Incapacitation ending concentration was subsequently added by the
  conditions slice below.
- Tests: `tests/agent/test_concentration.py` (seven cases added, scripted dice).


## EDG-043: exact monster conditions and verification recount

- Added the frightened and stunned hero subset, exact Frightful Presence/Mind Blast/short-rider
  parsing, same-source duration refresh, stable source identity and engine-owned repeat saves.
  Poisoned ability-check penalties now match the attack-roll penalty already supported.
- Incapacitation ends concentration and removes Dodge benefits, including persisted state.
  Movement and action checks use actual initiative-time conditions.
- The fixtures use synthetic creatures/numbers and scripted dice; source citations describe the
  selected 2014 wording, not a new human playtest or a whole-rulebook completeness claim.
- At that conditions-only checkpoint, authored lair execution, monster casting, surprise and
  Wing prone/flight were unfinished. The completion slice below supersedes that development list.
  Other condition wordings, 24-hour immunity across encounters and exact sight/ranges remain
  explicit limits of the supported subset.
- Verdict totals are recounted directly from all five current rule tables after the wave's merged
  slices and this reviewed conditions slice. Historical suite totals above remain dated receipts.

## EDG-043: reviewed lair, catalog casting and remaining named interactions

- Added explicit private lair-source bindings, conservative complete-option parsing for red-dragon
  magma/tremor, initiative-20 ordering after all ties and persisted nonrepetition. Other lair
  shapes remain declared; persistent cloud damage is never approximated by an initial substring.
- Added record-owned catalog monster casting, saved slots, legal action/bonus spell combinations,
  self-healing, marks, blindness and Hold Person. Source-owned concentration cannot remove
  another caster's effect and ends on damage failure, incapacitation, defeat, expiry and fight end.
- Added authored surprise, Wing Attack prone/flight, explicit hero stand-up recovery, hero
  paralysis turn gating, and critical weapon/spell hits from the melee band that still require
  a successful attack roll. Lair actions stop after the hero escapes.
  A blinded or incapacitated target imposes no nearby-hostile ranged disadvantage.
- Source probes used the installed books read-only. The Mage's extracted leading punctuation
  before Fire Bolt is accepted without truncating its cantrip list. Existing manifest receipts
  retain their digest when the new authoring fields are absent. Public/debug/narrator projections
  exclude unused spell lists and private source bindings.
- Verification is deterministic, using fake providers and disposable source/index fixtures. It
  establishes the named supported capability slices, not whole-rulebook or human play acceptance.
  The contract, README and these verdict tables describe the same limits.
