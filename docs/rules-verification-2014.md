# Rules verification against the 2014 rulebooks (EDG-149)

Scope: every code-owned rules table and rules text in the engine that was written from memory,
checked against the 2014 Player's Handbook (PHB), Dungeon Master's Guide (DMG) and Monster Manual
(MM) PDFs on the maintainer's machine. Those PDFs are gitignored and are not in this repository, so
this file cites them by PDF page number (`PDF p.N`, the page index in the PDF viewer, not the printed
folio) and quotes at most a few words. Stat blocks were read from the page images and transcribed to
a scratch file outside the repo; no rulebook passage is committed.

No 2024 book is available locally. 2024 numbers are not implemented (see "Unverifiable" below).

## Verdict counts

| Verdict | Rows | Meaning |
|---|---|---|
| MATCH | 45 | Engine value equals the book value at the cited page |
| FIXED | 23 | Discrepancy found and corrected (17 in EDG-149, 6 in EDG-152); a regression test pins the book value and fails on the old code |
| PARTIAL | 10 | Documented subset or known gap; the contract text states it |
| NOT-IN-BOOKS | 1 section | The encounter budget (section 6). Artificer data is also NOT-IN-BOOKS and is noted in the class rows |

The rows are the tables below (78 in total). A row may cover several values. EDG-152 turned two
PARTIAL rows (short-rest Hit Dice, prone) into `FIXED (EDG-152)`, split the Rage PARTIAL row into
two fixed rows and a smaller PARTIAL row, and added rows for Versatile weapons and armor Stealth
disadvantage. The PARTIAL rows that remain say in their note why the engine cannot execute them.

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

### Parser results on real Monster Manual stat blocks

The six entries were read from the page images (Young Red Dragon PDF p.99; Goblin and Goblin Boss
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
| Young Red Dragon | Multiattack, bite, claw, fire immunity, saves read. Fire Breath not declared. | Fire Breath (Recharge 5–6) declared. |

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
| Poisoned, frightened, paralyzed, stunned and the rest | not implemented | PHB Appendix A, PDF pp.291-293 | PARTIAL | Declared unsupported in the contract (unchanged). |
| Dodge: attacks against the hero have disadvantage until the next turn | `combatants.attack_roll_mode`, `graph_combat` | PHB p.193 ("Dodge"), PDF p.193 | PARTIAL | Engine applies the disadvantage. It does not check visibility, and does not give Dexterity saves advantage. Contract text corrected to say so. Left in EDG-152: no engine effect rolls a Dexterity save for the hero, and the only foe that cannot see the hero is a blinded one, whose own attack disadvantage already gives the same roll, so neither part changes an outcome today. |
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

## Tests and lint

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
