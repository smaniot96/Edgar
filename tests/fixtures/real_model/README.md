# Real-model fixtures (EDG-147)

Frozen outputs of the configured model, with human labels, used to measure the deterministic
boundary heuristics offline. No model is called by the tests or by `tools/eval_boundary.py`.

## Files

| File | Contents |
| --- | --- |
| `parse_fixtures.json` | 124 labelled player inputs (English, Italian, Spanish; `mode` explore / combat / captive) and the real `ParsedInput` from `agent.nodes.input_parser`; plus 10 scenario inputs. In-sample: used while the heuristics were being fixed. |
| `narration_fixtures.json` | 10 hand-built turns (kill round, nonlethal knockout, captured foe, two similar names, unsupported spell and maneuver, bait, a claimed advantage, a request to revive a dead foe), 3 real narrator samples each, and their human labels. In-sample. |
| `heldout_fixtures.json` | 30 held-out labelled inputs and 3 narration turns (9 samples) in which the player invites an unsupported effect. Labelled before its model run. |

Model `gpt-6-luna` (the configured `LLM_MODEL`), run 15 October 2026. Model calls made: 206 in total
(parser 167 logical calls, narrator 39), under the 300 budget. Parser calls use `max_tokens` 700, and
narrator calls use the production `NARRATOR_MAX_TOKENS` of 600. The parser's `max_tokens` was set by a
wrapper in the collection script, because production passes none. The collection script is not in the
repository. Each call is one logical call; SDK-level retries are not counted, and none errored.

No API key or `.env` value is stored in any fixture; `tests/agent/test_real_model_fixtures.py` checks it.

## Labelling rule (human judgement)

Each file has a `labelling_rule` (or `labelling_note`) field. In short:

- `expected.kinds`: the unsupported classifier kinds the declared action needs a ruling for. A catalog
  spell is supported only when the engine resolves it: its English name is in the declaration and the
  character has access. Idioms and figurative uses ("take advantage of", "cast my net", "block the
  door", "kill time") declare no mechanic.
- `expected.lethality`: only the intent the player declares in their own words for an attack in combat.
  Null when none, and for non-attacks.
- `flag: ambiguous` rows are kept but excluded from counts.
- Narration labels: `revives_or_invents_attacker` (an inactive foe made the subject of an attack, or an
  unlisted attacker named) and `claim_sentences` (sentences narrating an unsupported effect as happened).

Six in-sample rows were relabelled after the first baseline, each with a `label_note`: S07, S09, I10, I11
and L15 (catalog spells the character cannot cast, or named outside English, are unsupported) and L21
("stun" claims a stunned condition, as U09 does). U18 ("parry") is labelled as a feature, and U28 ("summon a spirit wolf") is flagged ambiguous and excluded. These are contract
facts, not heuristic outputs. The labels were written by one labeller, with no second rater, so agreement
is untested.

## Measurement

Baseline is the original `main` code (`a577eec`) on the same labels. Final is this branch. Counts are
over non-ambiguous rows. "bait" is the number of rows with an idiomatic or figurative phrase that produced
any ruling.

### Classifier (`classify_unsupported_actions`)

| Set | Code | Exact match | Bait with a ruling | spell | feature | maneuver | condition | advantage | action economy | positioning |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| in-sample (120) | baseline | 108 | 6 / 76 | p 0.93, fp 1 | p 0.89, fp 1 | p 0.50, fp 2 | p 0.80, fp 1 | r 0.60, fn 2 | r 0.75, fn 2 | p 0.75, fp 1, r 0.60, fn 2 |
| in-sample (120) | final | 120 | 0 / 76 | p 1.00, r 1.00 | p 1.00, r 1.00 | p 1.00, r 1.00 | p 1.00, r 1.00 | p 1.00, r 1.00 | p 1.00, r 1.00 | p 1.00, r 1.00 |
| held-out (30) | baseline | 26 | 1 / 17 | p 0.80, fp 1 | 1.00 | none | 1.00 | r 0.00, fn 1 | r 0.50, fn 1 | r 0.50, fn 1 |
| held-out (30) | final | 30 | 0 / 17 | p 1.00, r 1.00 | p 1.00, r 1.00 | none | p 1.00, r 1.00 | p 1.00, r 1.00 | p 1.00, r 1.00 | p 1.00, r 1.00 |

Held-out before the two generic idiom and hyphen fixes ("cast an eye", "bonus-action"): 28 of 30 exact.

Language: final in-sample English 99/99, Spanish 10/10, Italian 11/11 exact. Baseline English 92/99,
Spanish 8/10, Italian 8/11.

### Lethality (`declared_lethality`, `captives` channel)

| Set | Channel | Baseline | Final |
| --- | --- | --- | --- |
| in-sample (120) | parser field alone | 118 correct, fp 2 (U27, L04) | 118 correct, fp 2 (unchanged: the field is not trusted alone) |
| in-sample (120) | production (field, then words) | 116 correct, fp 4 (U27, B12, B15, L04) | 119 correct, fp 1 (U27) |
| in-sample (120) | words only (captives) | 108 correct, fp 2, fn 8, wrong 2 | 120 correct |
| held-out (30) | parser field alone | 29 correct, fp 1 (H11) | 29 correct, fp 1 (unchanged) |
| held-out (30) | production (field, then words) | 28 correct, fp 2 (H11, H25) | 29 correct, fp 1 (H11) |
| held-out (30) | words only (captives) | 26 correct, fp 1, fn 3 | 30 correct |

### Roster check (`roster_violations`) on real narrations

| Set | Baseline | Final |
| --- | --- | --- |
| in-sample (30 samples) | fp 2 (the surviving "Raider" called "the remaining Raider"; "the other goblin remains poised" with no attack) | fp 0 |
| held-out (9 samples) | fp 0 | fp 0 |

No real narration revived a dead, unconscious or captured foe, and no narration invented an attacker, in
39 real samples. That includes samples where the player asked the foe to rise. The true-positive rate is
therefore unmeasured on real output; the synthetic cases in the test file cover the mechanism.

### Narration sanitizer (`apply_narration_boundary`)

| Set | Baseline | Final |
| --- | --- | --- |
| in-sample | removed 6 sentences, all benign (fp 6), from 4 unsupported turns | removed 0, benign fp 0 |
| held-out | removed 2: one benign fp ("the magic finds no purchase"), one claim (tp) | removed 1 claim (tp), benign fp 0 |

Claim sentences in real output are rare: 1 sentence in 39 samples, so the false-negative rate is not
measured with any power.

### Adjudication sanitizer (`apply_adjudication_boundary`), engine roll lines

| Set | Baseline | Final |
| --- | --- | --- |
| in-sample | 5 of 5 engine roll lines lost | 0 of 4 lost |
| held-out | 3 of 3 lost | 0 of 3 lost |

The baseline counts are from the first run, which used the original roll-line pattern. The final run
keeps a line only when it has an engine format (`(d20 rolled N)`, `contest:`, `-> success|failure`, or
`NdN+M = total vs AC|DC`).

## Heuristic changes (summary)

- `supported_rules.py`: idiom pre-pass for word patterns; advantage, cover, disengage and bonus-action
  wording (English, Italian, Spanish); bare "lancio" and "lanzo" are no longer casting words; a parser-named
  catalog spell with no English name is ruled as not resolved; a parser-named combat action (Dash,
  Disengage, reaction) is classified in combat; pin and shove at inanimate objects are not maneuvers; blocking
  a door is not positioning; a negated spell mention is a ruling, not a claim; engine roll lines are kept in
  the adjudication summary by their engine formats only.
- `combatants.py`: Italian and Spanish intent words; "kill time" and "kill the lights" are not lethal; "not to
  kill", "unconscious", "senseless", "prisoner" are nonlethal cues; a parser-only lethal intent is not
  inferred; "the remaining X" is not an attacker, and "other X" counts only when followed by an attack verb.
- Contract text: `DECLARATION_LANGUAGE` (English first) in the payload and the README; the Blindness/Deafness
  and spell scope says a spell resolves only when its English name is in the declaration.

## Remaining misses (not fixed)

- Parser-only nonlethal intent: "I tackle the goblin to the ground." (U27) and "I knock the goblin captain down
  with my shield." (H11) are read as nonlethal. The EDG-022 contract pins that a parser-declared nonlethal
  stands alone. The error is toward a knockout, not a kill.
- Text-only path: "I dash toward the goblin captain." gets no ruling without the parser's `action` entity
  (it has one in the fixtures). Dash is caught only through the parser.
- Italian and Spanish spell names: "Lancio Dardo Magico" (Magic Missile) is ruled "not resolved", because the
  engine needs the English name in the text. This is honest, but it means an Italian player's catalog spell is
  not cast. The fix belongs to the engine's name resolution, which this story does not change.
- Engine, outside the classifier: `agent.class_features.requested_features` reads "rage against the machine" as
  a Rage declaration. The classifier now ignores it, but the engine round does not. `class_features.py` is not
  among this story's files.
- Parser-named feature for a non-feature: "parry" is named a feature by the parser. It is ruled, but it is not a
  5e feature.
- Sample size: 120 and 30 inputs, 39 narrations, one labeller. The held-out set is no longer clean after two
  generic fixes. A new held-out set is needed before a rate is quoted as a population estimate.

## Reproduce

```
uv run python tools/eval_boundary.py          # both sets, no model call
uv run pytest tests/agent/test_real_model_fixtures.py -q
```
