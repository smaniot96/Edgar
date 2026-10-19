"""Party combat: one encounter shared by several human-controlled heroes (EDG-049).

A party fight is a single initiative order of every hero and every foe. The engine still resolves one
human turn per request: the request of the hero whose slot is next resolves that slot, then runs every
slot after it automatically (foes, heroes who cannot act, a hero that is down) until the next hero who
can act, and pauses there. `combat_state.current_turn_index` is that pause point: the index of the next
slot in the initiative order, persisted in the existing column. Nothing else is stored: each hero's
hit points, conditions, reaction, action economy, concentration and death record live on that hero's
own combatant row (`character_id` ties the row to the sheet).

Rules the engine owns (documented in docs/party-combat.md):

* Foes choose one standing hero per turn with one engine die (`1dN` over the standing heroes in
  initiative order); with a single standing hero no die is rolled. When the hero it is attacking falls,
  the rest of its attacks go to another standing hero. A foe never attacks a hero at 0 HP, exactly as
  in a solo fight (`EnemyTurns.enemy_turn`).
* A hero that is down rolls its own death save at the start of its own slot, automatically (death
  saves mode), while another hero acts. Under the subdued mode a downed hero simply stays down and is
  left at 1 HP when the encounter ends.
* An ally's healing spell or potion that names another hero restores that hero (a revival when the
  hero was at 0 HP: its death record is cleared).
* A hero that leaves the fight (flee) is out of it; the others fight on. The encounter ends when no
  foe stands (victory), when no hero can act and none is dying (defeat, or `fled` if any hero
  escaped), or at the round cap (`fled`).

A `RoundRecord` is bound to one hero at a time (`PartyRound.bind`): every engine routine that reads
`rec.player`, `rec.character`, `rec.hp` runs unchanged against the bound hero. `unbind` writes the
values back to the hero's `Seat`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.action_economy import DODGE, economy_record
from agent.combat_declaration import Declaration
from agent.combat_enemy_turns import EnemyTurns
from agent.combat_finalise import close_round_effects
from agent.combat_player_phase import (
    PlayerTurnInput,
    finish_hero_turn,
    fire_readied,
    run_player_turn,
)
from agent.combat_round_record import RoundRecord
from agent.combat_view import _display
from agent.combatants import (
    apply_player_disposition,
    can_act,
    disposition_of,
    has_effect,
    is_standing,
)
from agent.conditions import drop_concentration_if_incapacitated, end_of_source_turn
from agent.death import DEATH_MODE_SAVES, DYING, record_from
from agent.models.parsed_input import ParsedInput
from agent.positioning import MovePlan
from agent.resolution import DEATH_SAVES_KEY
from tools.dice import roll

PC_PREFIX = "pc-"
DEFAULT_TURN_TEXT = "{hero} takes the Dodge action."


def pc_name(character_id: int) -> str:
    """The stable combatant id of a hero in a party fight."""
    return f"{PC_PREFIX}{int(character_id)}"


def hero_rows(combatants: list[dict] | None) -> list[dict]:
    return [c for c in combatants or [] if c.get("is_player")]


def is_party(combatants: list[dict] | None) -> bool:
    """A party fight has at least two hero rows. One hero (solo, or one seat) is the classic fight."""
    return len(hero_rows(combatants)) >= 2


def holder_row(combat_state: dict | None) -> dict | None:
    """The hero row whose slot is next in a running party fight, or None."""
    if not combat_state or combat_state.get("ended"):
        return None
    combatants = combat_state.get("combatants") or []
    if not is_party(combatants):
        return None
    index = combat_state.get("current_turn_index")
    if type(index) is not int or not 0 <= index < len(combatants):
        return None
    row = combatants[index]
    return row if row.get("is_player") and row.get("character_id") is not None else None


def holder_character_id(combat_state: dict | None) -> int | None:
    """The character whose turn it is in a running party fight (the combat turn holder), or None."""
    row = holder_row(combat_state)
    return int(row["character_id"]) if row is not None else None


@dataclass
class Seat:
    """One hero of the fight: its row, sheet and the values a `RoundRecord` carries while it is bound."""

    row: dict
    character: dict
    hp: int
    hp_start: int
    sw_left: int
    ac: int
    dead: bool = False

    @property
    def character_id(self) -> int:
        return int(self.row["character_id"])

    @property
    def name(self) -> str:
        return _display(self.row)


def empty_declaration(*, dodge: bool = False) -> Declaration:
    """A declaration with nothing declared (a hero that cannot act), or the default Dodge."""
    return Declaration(
        is_attack=False,
        is_spell=False,
        spell_declared=False,
        spell_plan=None,
        bonus_spell=None,
        dodge=dodge,
        dash=False,
        disengage=False,
        moving=False,
        standing_up=False,
        wants_second_wind=False,
        rage_declared=False,
        heal_item=None,
        missing_item=False,
        combat_action=None,
        declared=[DODGE] if dodge else [],
        target=None,
        release_target=None,
        spell_target=None,
        spell_targets=[None],
        moves=MovePlan(),
        attack_ranged=False,
        off_weapon=None,
    )


class PartyRound:
    """The heroes of a party fight for one request, and the loop that runs the slots between humans."""

    def __init__(self, rec: RoundRecord, seats: list[Seat], *, max_rounds: int) -> None:
        self.rec = rec
        self.seats = seats
        self.max_rounds = max_rounds
        self.current: Seat | None = None
        self.stalemate = False
        rec.party = self

    # --- binding -----------------------------------------------------------------------------

    def seat_for_row(self, row: dict) -> Seat | None:
        return next((s for s in self.seats if s.row is row), None)

    def seat_for_character(self, character_id: int | None) -> Seat | None:
        return next((s for s in self.seats if s.character_id == character_id), None)

    def bind(self, seat: Seat) -> None:
        """Make `seat` the hero the record's engine routines act on. The previous hero is flushed first."""
        if self.current is seat:
            return
        self.unbind()
        rec = self.rec
        rec.hero = seat.name
        rec.character = seat.character
        rec.player = seat.row
        rec.player_ac = seat.ac
        rec.hp = seat.hp
        rec.sw_left = seat.sw_left
        rec.dead = seat.dead
        self.current = seat

    def unbind(self) -> None:
        seat = self.current
        if seat is None:
            return
        rec = self.rec
        seat.hp = rec.hp
        seat.sw_left = rec.sw_left
        seat.dead = seat.dead or rec.dead
        rec.dead = False
        self._sync_row(seat)
        self.current = None

    def _sync_row(self, seat: Seat) -> None:
        row = seat.row
        row["hp_current"] = seat.hp
        if seat.dead:
            row["disposition"] = "dead"
            row["alive"] = False
        else:
            apply_player_disposition(row, subdued=False)

    def live_hp(self, seat: Seat) -> int:
        return self.rec.hp if seat is self.current else seat.hp

    def standing(self) -> list[Seat]:
        """Heroes still in the fight above 0 HP, in initiative order."""
        return [
            s
            for s in self.seats
            if self.live_hp(s) > 0
            and not (s.dead or (s is self.current and self.rec.dead))
            and not s.row.get("fled")
        ]

    # --- foes choose heroes ------------------------------------------------------------------

    def _pick(self, foe: dict, pool: list[Seat]) -> Seat:
        if len(pool) == 1:
            return pool[0]
        outcome = roll(f"1d{len(pool)}")
        self.rec.dice_log.append(outcome.to_log(f"{foe['name']} chooses a target"))
        return pool[outcome.total - 1]

    def pick_target(self, foe: dict) -> Seat | None:
        pool = self.standing()
        return self._pick(foe, pool) if pool else None

    def retarget(self, foe: dict, turns: EnemyTurns) -> bool:
        """The hero a foe was attacking is down: bind another standing hero. False when none stands."""
        pool = [s for s in self.standing() if s is not self.current]
        if not pool:
            return False
        self.bind(self._pick(foe, pool))
        return True

    # --- slots -------------------------------------------------------------------------------

    def foe_slot(self, turns: EnemyTurns, foe: dict) -> None:
        """A foe's slot: its lair boundary, one target choice, its turn, then the end-of-turn options."""
        turns.before_initiative_slot(foe)
        acts = is_standing(foe) and can_act(foe) and not foe.get("surprised")
        seat = self.pick_target(foe) if acts else None
        self.bind(seat or self.current or self.seats[0])
        turns.enemy_turn(foe)
        turns.another_creature_turn_ended(foe)
        turns.after_initiative_slot(foe)
        # A condition that lasts until the end of this foe's next turn ends for every hero it afflicted.
        round_num = int(self.rec.combat_state.get("round", 1) or 1)
        bound = self.current
        for seat in self.seats:
            if seat is bound:
                continue
            for kind in end_of_source_turn(seat.row, foe, round_num):
                self.rec.log_lines.append(f"{seat.name} is no longer {kind}.")
        self.unbind()

    def fire_readied_all(self, foe: dict, turns: EnemyTurns) -> None:
        """A foe closed into melee: every hero holding a readied attack against it may take it now."""
        previous = self.current
        for seat in self.seats:
            self.bind(seat)
            fire_readied(self.rec, turns, foe)
            if seat.row.get("reaction_used"):
                economy = dict(seat.row.get("action_economy") or {})
                economy["reaction"] = "readied attack"
                seat.row["action_economy"] = economy
        if previous is not None:
            self.bind(previous)

    def auto_slot(self, turns: EnemyTurns, seat: Seat, *, dodge: bool = False) -> None:
        """A hero's slot that needs no human: a death save, a hero that cannot act, or the default Dodge."""
        rec = self.rec
        self.bind(seat)
        if ended := drop_concentration_if_incapacitated(seat.row, rec.enemies):
            rec.log_lines.append(f"{seat.name}'s concentration on {ended} ends while incapacitated.")
        if dodge:
            rec.log_lines.append(DEFAULT_TURN_TEXT.format(hero=seat.name))
        turn = run_player_turn(
            rec,
            PlayerTurnInput(
                decl=empty_declaration(dodge=dodge),
                player_input="",
                parsed=ParsedInput(),
                hp_max=int(seat.row.get("hp_max") or seat.character.get("hp_max") or rec.hp or 1),
                cf_requested=(),
                cf_left={},
                turns=turns,
            ),
        )
        finish_hero_turn(rec, turns)
        seat.row["action_economy"] = economy_record(
            rec.round_num,
            turn.declared if turn.player_acted else [],
            dodging=has_effect(seat.row, "dodging"),
        )
        self.unbind()

    def can_pause(self, seat: Seat) -> bool:
        """Whether the slot waits for its human: the hero is up and unhindered."""
        return seat.hp > 0 and can_act(seat.row) and not seat.row.get("surprised")

    # --- end conditions ----------------------------------------------------------------------

    def decided(self) -> bool:
        if self.stalemate:
            return True
        if not any(is_standing(e) for e in self.rec.enemies):
            return True
        return not self._party_alive()

    def _party_alive(self) -> bool:
        """Some hero can still act, or is dying and may yet wake (a natural 20 on a death save)."""
        for seat in self.seats:
            row = seat.row
            if seat.dead or row.get("fled"):
                continue
            if self.live_hp(seat) > 0:
                return True
            if self.rec.death_mode == DEATH_MODE_SAVES:
                record = record_from(row.get(DEATH_SAVES_KEY))
                if record is None or record.state == DYING:
                    return True
        return False

    def end_round(self, turns: EnemyTurns) -> None:
        """The round is over: lair actions still owed, timed effects, then the next round (or the cap)."""
        rec = self.rec
        if not rec.lair_done:
            turns.lair_boundary()
        for seat in self.seats:
            self.bind(seat)
            close_round_effects(rec)
        self.unbind()
        if rec.round_num >= self.max_rounds:
            self.stalemate = True
            return
        rec.round_num += 1
        rec.combat_state["round"] = rec.round_num
        rec.lair_done = False

    def run_slots(self, turns: EnemyTurns, start: int) -> tuple[Seat | None, int]:
        """Resolve slots from `start` until a hero must decide (returned with its index) or the fight is decided.

        Foes act, heroes that cannot act take their automatic slot, fled and dead heroes are skipped.
        """
        order = self.rec.combatants
        cursor = start
        while not self.decided():
            if cursor >= len(order):
                self.end_round(turns)
                cursor = 0
                continue
            slot = order[cursor]
            if not slot.get("is_player"):
                self.foe_slot(turns, slot)
            else:
                seat = self.seat_for_row(slot)
                if seat is not None and not seat.dead and not slot.get("fled"):
                    if self.can_pause(seat):
                        return seat, cursor
                    self.auto_slot(turns, seat)
            cursor += 1
        return None, min(cursor, max(len(order) - 1, 0))

    # --- healing an ally ---------------------------------------------------------------------

    def heal_ally(self, ally_row: dict, amount: int) -> str:
        """Restore `amount` HP to another hero. Returns the engine's sentence about it.

        Healing that leaves a hero above 0 HP ends unconsciousness and clears the death record (a
        revival). A dead hero is beyond healing.
        """
        seat = self.seat_for_row(ally_row)
        if seat is None or seat.dead:
            return f" {_display(ally_row)} is beyond healing."
        before = self.live_hp(seat)
        hp_max = int(seat.row.get("hp_max") or seat.character.get("hp_max") or before)
        after = min(hp_max, before + amount)
        seat.hp = after
        row = seat.row
        row["hp_current"] = after
        revived = before <= 0 < after
        if after > 0:
            row.pop(DEATH_SAVES_KEY, None)
            row[DEATH_SAVES_KEY] = None
            apply_player_disposition(row, subdued=False)
        if revived:
            self.rec.round_effects.append("ally_revived")
        tail = " and is conscious again" if revived else ""
        return f" {seat.name} regains {after - before} HP{tail} ({after}/{hp_max})."


def outcome_of(party: PartyRound, rec: RoundRecord) -> tuple[bool, str | None]:
    """How a party fight stands after its last slot: (ended, outcome)."""
    if not any(is_standing(e) for e in rec.enemies):
        return True, "victory"
    if not party._party_alive():
        escaped = any(s.row.get("fled") for s in party.seats)
        return True, "fled" if escaped else "defeat"
    if party.stalemate:
        return True, "fled"
    return False, None


def party_updates(party: PartyRound, *, skip_character_id: int | None) -> list[dict[str, Any]]:
    """Sheet changes the round made to heroes the turn's own adjudication does not write.

    Each entry is `{character_id, hp_delta, death_saves}`; `death_saves` is the record's dict, or {}
    when the hero is conscious (clears a stored one). Heroes whose HP and death record did not change
    are left out, so a quiet round writes nothing.
    """
    out: list[dict[str, Any]] = []
    saves = party.rec.death_mode == DEATH_MODE_SAVES
    for seat in party.seats:
        if seat.character_id == skip_character_id:
            continue
        delta = seat.hp - seat.hp_start
        record = record_from(seat.row.get(DEATH_SAVES_KEY)) if saves else None
        stored = record_from(((seat.character or {}).get("stats") or {}).get(DEATH_SAVES_KEY))
        if delta == 0 and (not saves or record == stored):
            continue
        entry: dict[str, Any] = {"character_id": seat.character_id, "hp_delta": delta}
        if saves:
            entry["death_saves"] = record.to_dict() if record is not None else {}
        out.append(entry)
    return out


def finish_party_fight(party: PartyRound, *, ended: bool, outcome: str | None) -> list[str]:
    """Apply the end of the encounter to every hero row. Returns lines for the round summary."""
    lines: list[str] = []
    rec = party.rec
    party.unbind()
    if outcome == "victory":
        lines.append("All enemies are defeated. Victory!")
    elif outcome == "defeat":
        lines.append("Every hero is down. The party is overwhelmed.")
    elif outcome == "fled" and party.stalemate:
        lines.append("The fight drags on; the party disengages to regroup.")
    elif outcome == "fled":
        lines.append("The party breaks away from the fight and escapes.")
    if ended:
        for seat in party.seats:
            row = seat.row
            if seat.hp <= 0 and not seat.dead and rec.death_mode != DEATH_MODE_SAVES:
                # No human GM adjudicates death: a downed hero is subdued and left at 1 HP (EDG-045).
                seat.hp = 1
                row["hp_current"] = 1
                apply_player_disposition(row, subdued=True)
            row.pop("raging", None)
            row.pop("rage_round", None)
            row.pop("concentration", None)
    return lines


def disposition_label(row: dict) -> str:
    if row.get("fled"):
        return "left the fight"
    return disposition_of(row)


def party_roster(state: dict[str, Any]) -> list[dict]:
    """The sheets that take part in a new fight: the acting hero first, then the seated party.

    A dead hero does not fight. Fewer than two sheets means the classic solo fight.
    """
    from agent.death import life_state_of

    seen: dict[int, dict] = {}
    for sheet in [state.get("character"), *(state.get("party") or [])]:
        if not isinstance(sheet, dict) or sheet.get("id") is None:
            continue
        if life_state_of(sheet.get("stats")) == "dead":
            continue
        seen.setdefault(int(sheet["id"]), sheet)
    return list(seen.values())


def hero_row(sheet: dict, *, ac: int, surprised: bool) -> dict:
    """A new party fight's row for one hero. Initiative is rolled by the caller."""
    hp = int(sheet.get("hp_current") or 0)
    return {
        "name": pc_name(sheet["id"]),
        "display_name": sheet.get("name") or "Hero",
        "character_id": int(sheet["id"]),
        "is_player": True,
        "hp_current": hp,
        "hp_max": int(sheet.get("hp_max") or hp),
        "ac": ac,
        "disposition": "active" if hp > 0 else "unconscious",
        "alive": True,
        "surprised": surprised,
    }


def settle_party_state(
    party: PartyRound,
    acting: Seat,
    *,
    holder: Seat | None,
    cursor: int,
    ended: bool,
    outcome: str | None,
    sw_used: bool,
) -> None:
    """Write the combat state the API persists after a party request (the party twin of `settle_round`)."""
    from agent.combat_view import initiative_view

    rec = party.rec
    cs, combatants = rec.combat_state, rec.combatants
    if ended:
        from agent.combatants import clear_effects

        clear_effects(combatants)
    view = initiative_view(combatants)
    shown = holder.row if holder is not None else acting.row
    cs.update(
        {
            "combatants": combatants,
            "initiative_order": [c["name"] for c in combatants],
            "initiative": view,
            "economy": acting.row.get("action_economy"),
            "round": rec.round_num,
            "ended": ended,
            "outcome": outcome,
            "player_ac": acting.ac,
            "second_wind_used": sw_used,
            "second_wind_uses_remaining": rec.sw_left,
            "raging": bool(acting.row.get("raging")),
            "current_turn_index": cursor,
            "current_turn": None if ended else _display(shown),
            "current_turn_character_id": None if ended or holder is None else holder.character_id,
        }
    )
