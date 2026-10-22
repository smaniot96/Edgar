"""The enemy phase of a combat round: foes act in initiative order (EDG-057 split).

Each foe's start-of-turn features, attacks, spells, reactions, legendary and lair actions, and the
conditions that end with its turn. Dice are rolled here through `tools.dice` in the same order as
before the split, so seeded rounds are unchanged. The round's shared state is `RoundRecord`.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent.action_economy import (
    ACTION,
    BONUS_ACTION,
)
from agent.class_features import (
    rage_resists,
)
from agent.combat_dice import _mode_log, _roll_with_mode
from agent.combat_round_record import RoundRecord
from agent.combat_setup import (
    EnemyStat as EnemyStat,
    _creature_source_reference as _creature_source_reference,
    _enemy_damage_expr,
    classify_disposition as classify_disposition,
)
from agent.combat_targeting import select_target as select_target  # re-exported (tests)
from agent.combat_view import _display
from agent.combatants import (
    add_effect,
    attack_roll_mode,
    can_act,
    has_effect,
    is_standing,
    save_auto_fails,
    save_roll_mode,
    stable_id,
)
from agent.conditions import (
    afflict,
    apply_spell_condition,
    concentration_save,
    consume_mark,
    drop_concentration,
    drop_concentration_if_incapacitated,
    end_of_source_turn,
    end_of_turn_saves,
    rider_save,
    rider_save_line,
    save_line,
    save_modifier,
    spell_condition_sentence,
    start_concentration,
)
from agent.death import DEAD, DEATH_MODE_SAVES, record_from, take_damage
from agent.monster_stats import (
    AttackRider,
    AttackSave,
    MonsterAttack,
    attack_plan,
    legendary_plan,
    recharge_turn_start,
    regenerate,
    spend_recharge,
    stats_of,
)
from agent.positioning import (
    MELEE,
    MELEE_FEET,
    RANGED,
    RANGED_START_FEET,
    attacks_from_band,
    band_of,
    primary_melee_attack,
    start_turn,
)
from agent.resolution import (
    DEATH_SAVES_KEY,
    spend_second_wind,
)
from agent.spells import (
    SpellSpec,
    catalog_spell,
    resolve_monster_spell,
)
from tools.dice import parse_dice, roll


class EnemyTurns:
    """The enemy phase of one round: foes' attacks, spells, legendary and lair actions (EDG-057).

    Every method reads and writes the round's RoundRecord. Initiative order is decided by the caller,
    which calls these methods in the order the round resolves."""

    def __init__(self, rec: RoundRecord) -> None:
        self.rec = rec
        # EDG-044c: told when a foe closes from range into melee on its own turn, before its attacks.
        self.on_closed: Callable[[dict], None] | None = None
        # EDG-049: the party this round binds a target hero from (agent.party_combat), or None for solo.
        self.party: Any = None

    def _foe_closed(self, e: dict) -> None:
        if self.on_closed is not None:
            self.on_closed(e)

    # --- Shared HP bookkeeping (engine only) ---

    def damage_player(self, amount: int, *, critical: bool = False) -> None:
        if self.rec.death_mode == DEATH_MODE_SAVES:
            self._damage_hero_with_death_saves(amount, critical=critical)
            return
        self.rec.hp = max(0, self.rec.hp - amount)
        # EDG-043: a caster who falls to 0 HP loses concentration; other damage needs a CON save.
        if self.rec.hp <= 0:
            if ended_spell := drop_concentration(self.rec.player, self.rec.enemies):
                self.rec.log_lines.append(f"{self.rec.hero} falls and loses concentration on {ended_spell}.")
        elif (conc := concentration_save(self.rec.player, self.rec.enemies, self.rec.character, amount)) is not None:
            self.rec.dice_log.append(conc.outcome.to_log(f"{self.rec.hero} Constitution save (concentration)"))
            if conc.kept:
                self.rec.log_lines.append(
                    f"{self.rec.hero} keeps concentration on {conc.spell} (rolled {conc.total} vs DC {conc.dc})."
                )
            else:
                self.rec.log_lines.append(
                    f"{self.rec.hero} loses concentration on {conc.spell} (rolled {conc.total} vs DC {conc.dc})."
                )
        # No automatic rally unless a documented game mode enables it (default off).
        if self.rec.second_wind_auto_at_zero_hp and self.rec.hp <= 0 and not self.rec.fled:
            sw = spend_second_wind(self.rec.character, remaining=self.rec.sw_left)
            if sw is not None:
                self.rec.dice_log.append(sw.dice.to_log("second wind"))
                self.rec.hp = sw.amount
                self.rec.sw_left -= 1
                self.rec.log_lines.append(f"{self.rec.hero} digs deep and uses Second Wind, recovering {sw.amount} HP to stay in the fight!")

    def _damage_hero_with_death_saves(self, amount: int, *, critical: bool) -> None:
        """Damage to the hero under the death rules (EDG-045, agent.death; PHB, PDF p.198)."""
        rec = self.rec
        hero_max = int(rec.character.get("hp_max") or rec.player.get("hp_max") or max(1, rec.hp))
        before = rec.hp
        out = take_damage(
            before,
            hero_max,
            amount,
            critical=critical,
            record=record_from(rec.player.get(DEATH_SAVES_KEY)),
        )
        rec.hp = out.hp
        rec.player[DEATH_SAVES_KEY] = out.record.to_dict() if out.record is not None else None
        if out.instant_death:
            rec.dead = True
            rec.log_lines.append(
                f"{rec.hero} takes damage that equals or exceeds the hit point maximum and dies outright."
            )
        elif out.record is not None and out.record.state == DEAD:
            rec.dead = True
            rec.log_lines.append(f"{rec.hero} fails the third death saving throw and dies.")
        elif out.dropped:
            rec.log_lines.append(f"{rec.hero} is reduced to 0 HP and falls unconscious.")
        elif out.failures_added:
            count = "two failures" if critical else "a failure"
            rec.log_lines.append(f"{rec.hero} takes damage at 0 HP: {count} on the death saves.")
        if rec.hp <= 0 and not rec.dead:
            if ended_spell := drop_concentration(rec.player, rec.enemies):
                rec.log_lines.append(f"{rec.hero} falls and loses concentration on {ended_spell}.")

    def monster_concentration_damage(self, caster: dict, amount: int) -> None:
        """Apply a monster's Constitution concentration save after it takes damage."""
        if amount <= 0 or not caster.get("concentration"):
            return
        if int(caster.get("hp_current") or 0) <= 0:
            if ended := drop_concentration(caster, [self.rec.player]):
                self.rec.log_lines.append(f"{_display(caster)} falls and loses concentration on {ended}.")
            return
        stats = stats_of(caster)
        bonus = stats.save_bonus("CON") if stats is not None else 0
        dc = max(10, int(amount) // 2)
        outcome = roll(f"1d20{bonus:+d}" if bonus else "1d20")
        self.rec.dice_log.append(outcome.to_log(f"{_display(caster)} Constitution save (concentration)"))
        if outcome.total < dc:
            ended = drop_concentration(caster, [self.rec.player])
            if ended:
                self.rec.log_lines.append(
                    f"{_display(caster)} loses concentration on {ended} "
                    f"(rolled {outcome.total} vs DC {dc})."
                )
        else:
            concentration = caster.get("concentration") or {}
            self.rec.log_lines.append(
                f"{_display(caster)} keeps concentration on {concentration.get('spell')} "
                f"(rolled {outcome.total} vs DC {dc})."
            )


    def note_actor(self, e: dict) -> None:
        self.rec.round_actors.append(_display(e))
        if not any(a["id"] == stable_id(e) for a in self.rec.actors):  # one entry per foe (multiattack)
            self.rec.actors.append({"id": stable_id(e), "name": _display(e)})

    def enemy_attack(
        self,
        e: dict,
        *,
        label: str | None,
        bonus: int,
        expr: str,
        damage_type: str | None = None,
        exact: bool = False,
        rider: AttackRider | None = None,
        ranged: bool = False,
        within_five_feet: bool = False,
        reaction: bool = False,
    ) -> None:
        """One engine-rolled attack by an enemy. `exact` attacks keep their record's dice.

        `ranged` and `within_five_feet` (EDG-044) give the attack its reach disadvantage; `reaction`
        marks an opportunity attack, which spends the foe's reaction (see `opportunity_attack`).
        """
        self.note_actor(e)
        self.rec.events.append({
            "kind": "opportunity_attack" if reaction else "enemy_attack",
            "text": f"{_display(e)} {'makes an opportunity attack against' if reaction else 'attacks on its turn against'} {self.rec.hero}.",
            "actor_id": stable_id(e), "target_id": stable_id(self.rec.player),
        })
        mode = attack_roll_mode(e, self.rec.player, ranged=ranged, within_five_feet=within_five_feet)
        self.rec.round_modes.append(mode)
        atk, draws = _roll_with_mode(f"1d20{bonus:+d}", mode)
        consume_mark(self.rec.player)  # a Guiding Bolt mark grants advantage to the next attack roll only
        if reaction:
            purpose = f"{e['name']} opportunity attack"
        else:
            purpose = f"{e['name']} {label} attack" if label else f"{e['name']} attack"
        self.rec.dice_log.append(_mode_log(atk, purpose, mode, draws))
        mode_text = "" if mode == "normal" else f" at {mode}"
        subject = f"{e['name']}'s {label}" if label else e["name"]
        if reaction:
            subject = f"{subject} (opportunity attack)"
        natural = atk.rolls[0]
        paralyzed_crit = within_five_feet and has_effect(self.rec.player, "paralyzed")
        hit = natural == 20 or (natural != 1 and atk.total >= self.rec.player_ac)
        # EDG-045: an unconscious hero (0 HP, death saves) is hit critically from within 5 feet.
        unconscious_crit = (
            within_five_feet
            and self.rec.death_mode == DEATH_MODE_SAVES
            and self.rec.hp <= 0
            and not self.rec.dead
        )
        critical = hit and (natural == 20 or paralyzed_crit or unconscious_crit)
        if hit:
            if critical:
                p = parse_dice(expr)
                count = p.count * 2 if exact else min(p.count * 2, 8)
                expr = f"{count}d{p.sides}{p.modifier:+d}" if p.modifier else f"{count}d{p.sides}"
            dmg_roll = roll(expr)
            self.rec.dice_log.append(
                dmg_roll.to_log(f"{e['name']} {label} damage" if label else f"{e['name']} damage")
            )
            dmg = max(1, dmg_roll.total)
            resisted = ""
            if rage_resists(self.rec.character, damage_type, raging=bool(self.rec.player.get("raging"))):
                dmg //= 2
                resisted = ", halved by Rage resistance"
            crit = " Critical hit!" if critical else ""
            typed = f" {damage_type}" if damage_type else ""
            self.rec.log_lines.append(
                f"{subject} hits {self.rec.hero} for {dmg}{typed} damage{resisted} (rolled {atk.total}{mode_text} vs AC {self.rec.player_ac}).{crit}"
            )
            self.rec.outcomes.append(
                {
                    "actor": stable_id(e),
                    "action": "attack",
                    "attack": label,
                    "hit": True,
                    "roll": atk.total,
                    "roll_mode": mode,
                    "target_ac": self.rec.player_ac,
                    "critical": critical,
                    "damage": dmg,
                    "damage_type": damage_type,
                    **({"reaction": "opportunity_attack"} if reaction else {}),
                }
            )
            self.damage_player(dmg, critical=critical)
            if rider is not None and self.rec.hp > 0:
                # EDG-043: the exact record's rider on a hit: the hero's save, engine-rolled.
                rider_roll = rider_save(
                    self.rec.player,
                    rider,
                    character=self.rec.character,
                    source=_display(e),
                    source_id=stable_id(e),
                    round_applied=int(self.rec.combat_state.get("round", 1) or 1),
                )
                self.rec.dice_log.append(rider_roll.outcome.to_log(f"{self.rec.hero} {rider.ability} save ({rider.kind})"))
                self.rec.log_lines.append(rider_save_line(self.rec.hero, rider_roll, attack=label or e["name"]))
                if rider_roll.failed:
                    self.rec.round_effects.append(rider.kind)
                    if ended_spell := drop_concentration_if_incapacitated(self.rec.player, self.rec.enemies):
                        self.rec.log_lines.append(
                            f"{self.rec.hero}'s concentration on {ended_spell} ends while incapacitated."
                        )
        else:
            self.rec.log_lines.append(f"{subject} misses {self.rec.hero} (rolled {atk.total}{mode_text} vs AC {self.rec.player_ac}).")
            self.rec.outcomes.append(
                {
                    "actor": stable_id(e),
                    "action": "attack",
                    "attack": label,
                    "hit": False,
                    "roll": atk.total,
                    "roll_mode": mode,
                    "target_ac": self.rec.player_ac,
                    "critical": False,
                    "damage": 0,
                    "damage_type": damage_type,
                    **({"reaction": "opportunity_attack"} if reaction else {}),
                }
            )

    def enemy_save_action(self, e: dict, attack: MonsterAttack, save: AttackSave) -> None:
        """A breath-weapon style action (EDG-043): the hero's engine-rolled save, then the damage.

        A failed save takes the rolled damage and records the action's condition, if it has one; a
        success takes half of the damage (rounded down) or none, as the record states. A dodging
        hero has advantage on a Dexterity save (PHB, PDF p.193); a stunned hero fails a Strength or
        Dexterity save without a roll (PHB, PDF p.293).
        """
        self.note_actor(e)
        if save_auto_fails(self.rec.player, save.ability):
            saved, mode, total = False, "normal", None
            rolled = "automatic failure while stunned"
        else:
            mode = save_roll_mode(self.rec.player, save.ability)
            bonus = save_modifier(self.rec.character, save.ability)
            saving, draws = _roll_with_mode(f"1d20{bonus:+d}" if bonus else "1d20", mode)
            purpose = f"{self.rec.hero} {save.ability} save ({attack.name})"
            self.rec.dice_log.append(_mode_log(saving, purpose, mode, draws))
            saved, total = saving.total >= save.dc, saving.total
            rolled = f"rolled {total}" + ("" if mode == "normal" else f" at {mode}")
        dmg = 0
        if attack.damage_dice:
            dmg_roll = roll(attack.damage_dice)
            self.rec.dice_log.append(dmg_roll.to_log(f"{e['name']} {attack.name} damage"))
            dmg = dmg_roll.total
            if saved:
                dmg = dmg // 2 if save.half_on_success else 0
        result = "succeeds on" if saved else "fails"
        effects = []
        if attack.damage_dice:
            effects.append(f"takes {dmg} {attack.damage_type} damage" if dmg else "takes no damage")
        if save.condition:
            effects.append(f"is not {save.condition}" if saved else f"is {save.condition}")
        self.rec.log_lines.append(
            f"{self.rec.hero} {result} the {save.ability} save against {e['name']}'s {attack.name} "
            f"({rolled} vs DC {save.dc}) and {' and '.join(effects)}."
        )
        self.rec.outcomes.append(
            {
                "actor": stable_id(e),
                "action": "save",
                "attack": attack.name,
                "ability": save.ability,
                "dc": save.dc,
                "roll": total,
                "roll_mode": mode,
                "saved": saved,
                "damage": dmg,
                "damage_type": attack.damage_type,
                **({"condition": save.condition} if save.condition and not saved else {}),
            }
        )
        # Damage can trigger the usual concentration save first; a failed save that stuns the hero
        # then ends concentration regardless of whether that damage save succeeded.
        self.damage_player(dmg)
        if save.condition and not saved and self.rec.hp > 0:
            if save.condition == "prone":
                add_effect(
                    self.rec.player,
                    "prone",
                    source=_display(e),
                    round_applied=int(self.rec.combat_state.get("round", 1) or 1),
                )
            else:
                afflict(
                    self.rec.player,
                    save.condition,
                    source=_display(e),
                    round_applied=int(self.rec.combat_state.get("round", 1) or 1),
                    ability=save.ability,
                    dc=save.dc,
                    source_id=stable_id(e),
                )
            self.rec.round_effects.append(save.condition)
            if ended_spell := drop_concentration_if_incapacitated(self.rec.player, self.rec.enemies):
                self.rec.log_lines.append(f"{self.rec.hero}'s concentration on {ended_spell} ends while incapacitated.")

    def exact_attack(self, e: dict, attack: MonsterAttack, band: str) -> None:
        """One attack or save action of an exact record, made from the foe's band."""
        if attack.save is not None:
            self.enemy_save_action(e, attack, attack.save)
        else:
            self.enemy_attack(
                e,
                label=attack.name,
                bonus=attack.bonus,
                expr=attack.damage_dice,
                damage_type=attack.damage_type,
                exact=True,
                rider=attack.rider,
                ranged=attack.ranged,
                within_five_feet=band == MELEE,
            )

    def choose_monster_spell(self, e: dict, action_cost: str) -> tuple[SpellSpec, int] | None:
        """Choose an available exact catalog spell with the requested action cost."""
        stats = stats_of(e)
        casting = None if stats is None else stats.spellcasting
        if casting is None:
            return None
        spent = e.setdefault("monster_spell_slots_expended", {})
        if not isinstance(spent, dict):
            spent = e["monster_spell_slots_expended"] = {}
        candidates: list[tuple[int, int, SpellSpec]] = []
        for level, name in casting.spells:
            spec = catalog_spell(name)
            if spec is None or spec.level != level or spec.action != action_cost:
                continue
            if spec.condition and has_effect(self.rec.player, spec.condition):
                continue
            if spec.humanoid_only and not self.rec.player.get("humanoid", True):
                continue
            if spec.kind == "heal" and int(e.get("hp_current") or 0) >= int(e.get("hp_max") or 0):
                continue
            if level:
                available = int(casting.slots.get(str(level), 0) or 0)
                used = int(spent.get(str(level), 0) or 0)
                if used >= available:
                    continue
            # Prefer a new control effect, then the most powerful remaining spell. The fallback
            # attack is used once all recorded leveled slots are spent.
            priority = 2 if spec.kind == "condition" else 1 if spec.level else 0
            candidates.append((priority, level, spec))
        if not candidates:
            return None
        _priority, level, chosen = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
        if level:
            spent[str(level)] = int(spent.get(str(level), 0) or 0) + 1
        return chosen, level

    def cast_monster_spell(self, e: dict, spec: SpellSpec) -> None:
        """Resolve a catalog spell using the exact record-owned numbers."""
        stats = stats_of(e)
        casting = None if stats is None else stats.spellcasting
        if casting is None:
            return
        self.note_actor(e)
        source = spec.name
        source_id = f"{stable_id(e)}:{spec.name.casefold()}"
        if spec.concentration:
            if previous := drop_concentration(e, [self.rec.player]):
                self.rec.log_lines.append(f"{_display(e)}'s concentration on {previous} ends.")

        save_bonus = save_modifier(self.rec.character, spec.save_ability or "DEX")
        save_mode = save_roll_mode(self.rec.player, spec.save_ability or "DEX")
        if spec.save_ability == "DEX" and has_effect(self.rec.player, "restrained"):
            save_mode = "normal" if save_mode == "advantage" else "disadvantage"
        attack_mode = attack_roll_mode(
            e,
            self.rec.player,
            ranged=True,
            within_five_feet=band_of(e) == MELEE,
        )
        result = resolve_monster_spell(
            spec,
            attack_bonus=casting.attack_bonus,
            save_dc=casting.save_dc,
            caster_level=casting.caster_level,
            target_ac=self.rec.player_ac,
            target_save_bonus=save_bonus,
            healing_modifier=stats.abilities.get(casting.ability, 0),
            save_mode=save_mode,
            roll_mode=attack_mode,
            automatic_save_failure=(
                spec.save_ability is not None and save_auto_fails(self.rec.player, spec.save_ability)
            ),
            critical_on_hit=band_of(e) == MELEE and has_effect(self.rec.player, "paralyzed"),
        )
        if spec.kind == "attack":
            consume_mark(self.rec.player)  # the spell attack roll spends a mark against its target
        self.rec.dice_log.extend(result.dice_log)
        self.rec.log_lines.extend(spec.declared)
        if result.healing:
            before_hp = int(e.get("hp_current") or 0)
            e["hp_current"] = min(int(e.get("hp_max") or before_hp), before_hp + result.healing)
            self.rec.log_lines.append(
                f"{_display(e)} casts {spec.name} and regains {e['hp_current'] - before_hp} HP."
            )
        if result.damage:
            damage = result.damage
            if rage_resists(self.rec.character, spec.damage_type, raging=bool(self.rec.player.get("raging"))):
                damage //= 2
                self.rec.log_lines.append(f"Rage resistance halves the {spec.damage_type} damage.")
            self.damage_player(damage)
        if result.condition and result.save_success is False and self.rec.hp > 0:
            applied = apply_spell_condition(
                self.rec.player,
                result.condition,
                source=source,
                round_applied=self.rec.round_num,
                duration_rounds=spec.duration_rounds,
                repeat_save=(
                    {"ability": spec.save_ability, "dc": casting.save_dc}
                    if spec.save_ability
                    else None
                ),
                source_id=source_id,
            )
            if applied:
                self.rec.round_effects.append(result.condition)
                if spec.concentration:
                    start_concentration(
                        e,
                        source,
                        round_applied=self.rec.round_num,
                        duration_rounds=spec.duration_rounds,
                        source_id=source_id,
                    )
                if ended_spell := drop_concentration_if_incapacitated(self.rec.player, self.rec.enemies):
                    self.rec.log_lines.append(f"{self.rec.hero}'s concentration on {ended_spell} ends while incapacitated.")
            self.rec.log_lines.append(
                f"{_display(e)} casts {spec.name}. "
                + spell_condition_sentence(result.condition, self.rec.hero, applied=applied)
            )
        elif result.attack is not None:
            if spec.kind == "attack":
                self.rec.round_modes.append(attack_mode)
                if result.attack.success:
                    self.rec.log_lines.append(
                        f"{_display(e)} casts {spec.name}: the spell attack hits {self.rec.hero} for "
                        f"{result.damage} {spec.damage_type} damage."
                    )
                    if result.condition and self.rec.hp > 0:
                        applied = apply_spell_condition(
                            self.rec.player,
                            result.condition,
                            source=source,
                            round_applied=self.rec.round_num,
                            duration_rounds=spec.duration_rounds,
                            source_id=source_id,
                        )
                        if applied:
                            self.rec.round_effects.append(result.condition)
                        self.rec.log_lines.append(spell_condition_sentence(result.condition, self.rec.hero, applied=applied))
                else:
                    self.rec.log_lines.append(f"{_display(e)} casts {spec.name}, but the spell attack misses {self.rec.hero}.")
            elif result.damage:
                self.rec.log_lines.append(f"{_display(e)} casts {spec.name} at {self.rec.hero} for {result.damage} damage.")
        elif spec.kind in {"save", "condition"}:
            outcome = "succeeds" if result.save_success else "fails"
            rolled = (
                "automatically fails while incapacitated"
                if save_auto_fails(self.rec.player, spec.save_ability or "")
                else f"rolls against DC {casting.save_dc}"
            )
            self.rec.log_lines.append(
                f"{_display(e)} casts {spec.name}; {self.rec.hero} {outcome} the {spec.save_ability} save "
                f"({rolled})."
            )
            if result.damage:
                self.rec.log_lines.append(f"{self.rec.hero} takes {result.damage} {spec.damage_type} damage.")
        elif result.damage:
            self.rec.log_lines.append(
                f"{_display(e)} casts {spec.name} at {self.rec.hero} for {result.damage} "
                f"{spec.damage_type or 'damage'}."
            )
        else:
            self.rec.log_lines.append(f"{_display(e)} casts {spec.name}.")

    def monster_spell_action(self, e: dict) -> bool:
        """Use an action spell, with a legal bonus spell where the catalog allows both."""
        selected = self.choose_monster_spell(e, ACTION)
        if selected is not None:
            action_spell, _level = selected
            self.cast_monster_spell(e, action_spell)
            # A bonus-action spell permits another spell only when it is a cantrip.
            if action_spell.level == 0:
                if bonus := self.choose_monster_spell(e, BONUS_ACTION):
                    self.cast_monster_spell(e, bonus[0])
            return True
        if bonus := self.choose_monster_spell(e, BONUS_ACTION):
            self.cast_monster_spell(e, bonus[0])
        # Bonus actions leave the ordinary action available for the weapon/Multiattack plan.
        return False

    def foe_attacks(self, e: dict) -> None:
        stats = stats_of(e)
        if stats is None:
            # Legacy or generic combatant: its stored numbers, one attack (EDG-148 clamps apply).
            # Its one attack is a melee attack, so at range it closes in first (EDG-044).
            if band_of(e) == RANGED:
                e["band"] = MELEE
                if "distance_ft" in e:
                    e["distance_ft"] = MELEE_FEET
                self.rec.log_lines.append(f"{e['name']} closes to melee range.")
                self._record_close(e)
                self._foe_closed(e)
            self.enemy_attack(
                e,
                label=None,
                bonus=int(e.get("attack_bonus", 3)),
                expr=_enemy_damage_expr(e.get("damage_dice", "1d6+1")),
            )
            return
        if self.monster_spell_action(e):
            return
        # Exact record: make each listed attack in order after start-of-turn features resolve.
        plan = attack_plan(stats, spent=e.get("recharge_spent") or ())
        band = band_of(e)
        if band == RANGED and not any(a.ranged for a in plan):
            # A melee-only foe at range spends its movement to close in, then attacks (EDG-044).
            e["band"] = band = MELEE
            if "distance_ft" in e:
                e["distance_ft"] = MELEE_FEET
            self.rec.log_lines.append(f"{e['name']} closes to melee range.")
            self._record_close(e)
            self._foe_closed(e)
        plan = attacks_from_band(plan, band)
        if not plan:
            self.rec.log_lines.append(f"{e['name']} has no supported attack and takes no action.")
        for attack in plan:
            if self.rec.hp <= 0 and not self._retarget(e):
                break
            self.exact_attack(e, attack, band)
            spend_recharge(e, attack)  # a recharge attack waits for its d6 (no-op for the others)

    def _record_close(self, e: dict) -> None:
        self.rec.events.append({
            "kind": "enemy_close", "text": f"{_display(e)} closes to melee on its turn.",
            "actor_id": stable_id(e), "target_id": stable_id(self.rec.player),
        })

    def _retarget(self, e: dict) -> bool:
        """EDG-049: the hero a foe was attacking is down; the rest of its attacks go to another standing hero."""
        return self.party is not None and self.party.retarget(e, self)

    def _anyone_standing(self) -> bool:
        if self.party is not None:
            return bool(self.party.standing())
        return self.rec.hp > 0

    def opportunity_attack(self, e: dict) -> bool:
        """The foe's reaction: one melee attack against the hero as it leaves reach (PDF p.196).

        Uses the record's first melee attack, or the stored numbers of a legacy foe. Returns False
        when the foe has no melee attack to make.
        """
        if e.get("surprised"):
            return False
        stats = stats_of(e)
        if stats is None:
            self.enemy_attack(
                e,
                label=None,
                bonus=int(e.get("attack_bonus", 3)),
                expr=_enemy_damage_expr(e.get("damage_dice", "1d6+1")),
                reaction=True,
            )
        else:
            attack = primary_melee_attack(stats)
            if attack is None:
                return False
            self.enemy_attack(
                e,
                label=attack.name,
                bonus=attack.bonus,
                expr=attack.damage_dice,
                damage_type=attack.damage_type,
                exact=True,
                reaction=True,
            )
        e["reaction_used"] = True
        if "opportunity_attack" not in self.rec.round_effects:
            self.rec.round_effects.append("opportunity_attack")
        return True

    def legendary_action(self, e: dict) -> None:
        """Use at most one option at the end of this other creature's turn (MM, PDF p.12)."""
        stats = stats_of(e)
        if not self._anyone_standing() or stats is None or not stats.legendary or not can_act(e) or e.get("surprised"):
            return
        if self.party is not None and self.rec.hp <= 0 and not self.party.retarget(e, self):
            return
        band = band_of(e)
        left = stats.legendary_actions - int(e.get("legendary_spent") or 0)
        option = next(
            (o for o in legendary_plan(stats, left) if attacks_from_band([o.attack], band)), None
        )
        if option is None:
            return
        e["legendary_spent"] = int(e.get("legendary_spent") or 0) + option.cost
        self.rec.log_lines.append(
            f"{e['name']} uses a legendary action: {option.name} (costs {option.cost})."
        )
        self.exact_attack(e, option.attack, band)
        if option.flight_after and is_standing(e):
            e["band"] = RANGED
            if "distance_ft" in e:
                e["distance_ft"] = RANGED_START_FEET
            self.rec.log_lines.append(f"{e['name']} flies away after its Wing Attack.")

    def lair_action(self, e: dict) -> None:
        """Resolve one reviewed lair option at initiative 20, with ties lost.

        At a party table (EDG-049) the option is chosen once and every standing hero makes its own save
        against one shared damage roll (MM, PDF p.11); a solo round rolls exactly as before.
        """
        stats = stats_of(e)
        if self.rec.fled or not self._anyone_standing() or stats is None or not stats.lair_actions or not can_act(e) or e.get("surprised"):
            return
        previous = e.get("lair_last_action") if int(e.get("lair_last_round") or 0) == self.rec.round_num - 1 else None
        option = next((a for a in stats.lair_actions if a.name != previous), None)
        if option is None:
            return
        e["lair_last_action"] = option.name
        e["lair_last_round"] = self.rec.round_num
        self.note_actor(e)
        shared = None
        if self.party is not None and option.damage_dice:
            shared = roll(option.damage_dice)
            self.rec.dice_log.append(shared.to_log(f"{e['name']} {option.name} damage"))
        if self.party is None:
            self._lair_on_hero(e, option, shared)
            return
        for seat in self.party.standing():
            self.party.bind(seat)
            self._lair_on_hero(e, option, shared)
        self.party.unbind()

    def _lair_on_hero(self, e: dict, option: Any, shared: Any) -> None:
        """The lair option against the bound hero: its save, then the damage and the condition."""
        if save_auto_fails(self.rec.player, option.ability):
            saved, total, mode = False, None, "automatic failure while incapacitated"
        else:
            mode = save_roll_mode(self.rec.player, option.ability)
            bonus = save_modifier(self.rec.character, option.ability)
            saving, draws = _roll_with_mode(f"1d20{bonus:+d}" if bonus else "1d20", mode)
            self.rec.dice_log.append(_mode_log(saving, f"{self.rec.hero} {option.ability} save ({option.name})", mode, draws))
            total = saving.total
            saved = total >= option.dc
        dealt = 0
        if option.damage_dice:
            damage = shared
            if damage is None:
                damage = roll(option.damage_dice)
                self.rec.dice_log.append(damage.to_log(f"{e['name']} {option.name} damage"))
            dealt = damage.total if not saved else damage.total // 2 if option.half_on_success else 0
        self.rec.log_lines.append(
            f"At initiative 20, {e['name']} uses its lair action: {option.name}. "
            f"{self.rec.hero} {'succeeds' if saved else 'fails'} the {option.ability} save"
            f"{'' if total is None else f' (rolled {total} vs DC {option.dc})'}."
        )
        if option.condition == "prone" and not saved and self.rec.hp > 0:
            add_effect(self.rec.player, "prone", source=option.name, round_applied=self.rec.round_num)
            self.rec.round_effects.append("prone")
            self.rec.log_lines.append(f"{self.rec.hero} is knocked prone.")
        if dealt:
            self.rec.log_lines.append(f"{self.rec.hero} takes {dealt} {option.damage_type} damage from {option.name}.")
            self.damage_player(dealt)

    def another_creature_turn_ended(self, actor: dict) -> None:
        """Give every other able foe one legendary-action opportunity at this turn boundary."""
        if self.rec.fled or not self._anyone_standing() or not is_standing(actor):
            return
        for e in self.rec.enemies:
            if e is not actor:
                self.legendary_action(e)


    def lair_boundary(self) -> None:
        if self.rec.lair_done:
            return
        self.rec.lair_done = True
        for foe in self.rec.enemies:
            self.lair_action(foe)

    def before_initiative_slot(self, actor: dict) -> None:
        if not self.rec.lair_done and int(actor.get("init", 0) or 0) < 20:
            self.lair_boundary()

    def after_initiative_slot(self, actor: dict) -> None:
        if self.rec.lair_done or int(actor.get("init", 0) or 0) < 20:
            return
        index = self.rec.combatants.index(actor)
        if not any(int(next_actor.get("init", 0) or 0) >= 20 for next_actor in self.rec.combatants[index + 1 :]):
            self.lair_boundary()

    def enemy_turn(self, e: dict) -> None:
        start_turn(e)  # its reaction comes back at the start of its turn (PDF p.191)
        e.pop("legendary_spent", None)  # and its spent legendary actions (MM, PDF p.12)
        if ended_spell := drop_concentration_if_incapacitated(e, [self.rec.player]):
            self.rec.log_lines.append(f"{_display(e)}'s concentration on {ended_spell} ends while incapacitated.")
        # A foe that is down takes no turn. A paralyzed foe is still in the fight: it has its turn
        # (its recharge and its repeat save) but makes no attack.
        if not is_standing(e) or self.rec.hp <= 0 or self.rec.fled:
            return
        for name, outcome, recharged in recharge_turn_start(e):
            self.rec.dice_log.append(outcome.to_log(f"{e['name']} {name} recharge"))
            state_word = "recharges" if recharged else "does not recharge yet"
            self.rec.log_lines.append(f"{e['name']}'s {name} {state_word} (rolled {outcome.total} on the d6).")
        # Regeneration is a start-of-turn trait; it still applies if the creature cannot act.
        if stats_of(e) is not None:
            _healed, regen_note = regenerate(e)
            if regen_note:
                self.rec.log_lines.append(regen_note)
        if can_act(e) and not e.get("surprised"):
            self.foe_attacks(e)
        # EDG-043: a save-ending spell condition is repeated at the end of the foe's own turn.
        for save in end_of_turn_saves(e):
            self.rec.dice_log.append(save.outcome.to_log(f"{save.name} {save.ability} save ({save.kind})"))
            self.rec.log_lines.append(save_line(save))
        # EDG-043: a hero condition that lasts until the end of this foe's next turn ends with it.
        for kind in end_of_source_turn(self.rec.player, e, int(self.rec.combat_state.get("round", 1) or 1)):
            self.rec.log_lines.append(f"{self.rec.hero} is no longer {kind}.")
        e["surprised"] = False
