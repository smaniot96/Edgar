# Edgar

A solo Dungeon Master AI that runs a D&D 5e campaign and lets you play it from a chat UI in your browser. PostgreSQL holds all truth; an LLM proposes outcomes, the rules engine and dice tool decide them, and the database persists what happened.

## Stack

Python 3.12 with `uv` workspaces. FastAPI for the HTTP layer; Pydantic v2 schemas mirror SQLAlchemy 2.0 async ORM models. Postgres 16 stores users, campaigns, sessions, characters, NPCs, world flags, combat state, and an append-only event log; Alembic owns migrations. Qdrant stores embedded chunks of the rulebooks (PHB, DMG, MM) and the adventure module (Chalice of the Mountain God by default), with `text-embedding-3-small`. The agent is a LangGraph state machine (input parser, world retriever, rules adjudicator, world state updater, narrator, memory summarizer) with a combat subgraph that is invoked when intent classifies as combat. Redis carries a per-session turn lock so two requests cannot race a turn.

The chat UI is a React SPA under [`frontend/`](frontend/README.md), served by the API at `/ui` after `npm run build` (included in the Docker API image).

## Quick start

```
cp .env.example .env          # set OPENAI_API_KEY
make build                   # build API (including UI) and migration images
make up                      # migrate, start the runtime stack, wait, seed
open http://localhost:8000/ui
```

For local UI development with hot reload, run the API and Vite in two terminals (`make frontend-dev` uses port **5173** and proxies `/api` to the API):

```
make frontend-install          # once
# Terminal A (after make build):
make dev-up                    # current backend source + hot reload
# Terminal B:
make frontend-dev
open http://localhost:5173/ui/
```

`make up` runs Alembic (`make migrate`), brings the stack up, polls `/health` until the API answers (default 60s; override with `WAIT_TIMEOUT_SEC`), then hits `/api/seed`. Use `make logs` to tail the API container, `make down` to stop. `/health` is liveness only; `/ready` also checks Postgres, Redis and Qdrant (503 with a per-component breakdown if one is down).

Run `make build` again after changing dependencies or migrations, or to update the baked UI/backend in base mode. Development mode mounts current backend and migration source; dependency changes still need a rebuild. The API at port 8000 serves the UI built into its image; use Vite on port 5173 for frontend hot reload. When switching between base and development modes, run the corresponding `make up` or `make dev-up` to recreate services with that configuration.

### Compose services and profiles

`docker compose up -d` starts only the runtime stack: `api`, `db`, `redis`, `qdrant`. One-off jobs sit behind profiles and are run on demand (`docker compose run` enables a service's profile automatically):

| Service | Profile | How to run |
| --- | --- | --- |
| `migrate` | `tools` | `make migrate` (also part of `make up`) |
| `ingestion` | `tools` | `make ingest PDF=... COLLECTION=...` |
| `rules_engine` | `rules` | `make rules` (rules-only RAG demo) |

All services are published on `127.0.0.1` only, including the API on port 8000. Redis and Qdrant images are pinned (`redis:8.6.3`, `qdrant/qdrant:v1.16.3`); bump them deliberately. Images run as a non-root user and each Dockerfile target installs only its own workspace package (`uv sync --package ...`); the `api` image includes `ingestion` because uploads and AI-authored campaigns are embedded in-process.

Compose explicitly rotates container output with the `json-file` driver, retaining up to three 10 MB files per container. `make logs` reads this output; recreating a service applies changed logging options. PostgreSQL's `event_log` stores campaign history and adjudications as application data. Container log rotation does not delete that history.

`docker-compose.dev.yml` is a tracked, optional overlay: it mounts backend source, runs uvicorn with `--reload`, enables developer diagnostics, and mounts current migrations into the migration job. Select it with `make dev-up` or `docker compose -f docker-compose.yml -f docker-compose.dev.yml ...`. Matching `dev-build`, `dev-migrate`, `dev-logs` and `dev-down` targets use the same files. Plain Make targets select only `docker-compose.yml` and do not auto-load personal overrides. The former local `docker-compose.override.yml` was removed; Compose no longer automatically applies it.

Uploads and AI generation run as in-process background tasks. A restart marks unfinished work `interrupted` on startup; it does not resume a worker automatically. Generation saves its outline and completed chapters so an explicit retry can resume missing chapters or rebuild the index from a completed manuscript. PDF jobs retry from a retained upload or inspected extraction. A failed replacement leaves the last published source available, and `POST /api/adventures/{slug}/cancel` requests cancellation at the next safe boundary. Adventure status includes a safe job summary with the current stage, saved chapter count and next recovery action; it does not expose manuscript content or invent a percentage/ETA.

## Playing

Drop your PDFs into `data/pdfs/` (gitignored). Extract and inspect each complete rulebook
before indexing it, then send turns from the UI:

```
make ingest PDF=player_handbook.pdf INGEST_ARGS='--extract-only'
# Inspect data/markdown/player_handbook.md and its extraction JSON against the PDF.
make ingest PDF=player_handbook.pdf COLLECTION=rules_player_handbook INGEST_ARGS='--reviewed-extraction /output/markdown/player_handbook.extraction.json'
make ingest PDF=chalice_of_the_mountain_god.pdf COLLECTION=chalice_of_the_mountain_god
```

Repeat the extract/review steps for `dm_guide.pdf` and `monster_manual.pdf`, using their
matching `rules_*` collections and extraction bundles. `make ingest` runs the ingestion
container against the bind-mounted `data/pdfs/`; `INGEST_ARGS` passes CLI options through.
Rulebook indexing and PDF replacement (`--replace`) require a reviewed full-document
bundle. See `apps/ingestion/README.md` for diagnostics, recovery, collection naming and
campaign-bound notes (`--campaign-id`).

You can also play without a UI:

```
curl -N -X POST http://localhost:8000/api/sessions/1/turn/stream \
  -H 'content-type: application/json' \
  -d '{"message":"I look around the tavern"}'
```

The `/turn/stream` endpoint emits Server-Sent Events (`status`, `token`, `adjudication`, `done`, `error`); `/turn` returns one JSON. `apps/api/README.md` documents both.

Shared tables (token mode, several players in one campaign) take turns through a server-side
spotlight: only the holder can act, the holder's turn passes it on, and the rules (round robin,
free-for-all, owner-directed), the timeout and the owner's override are in
`docs/session-broadcast.md` (EDG-048). In a fight with two or more heroes the initiative order, not the
rotation, decides whose turn it is: one shared encounter, one human turn per request, foes between
heroes resolved by the engine, per-hero death saves, an idle hero skipped with a default Dodge
(`POST /api/sessions/{id}/combat/skip`); see `docs/party-combat.md` (EDG-049). Lobby and spotlight UI
are not in the browser client yet (EDG-050).

### Rules scope

The engine simulates a deliberately simplified subset of 5e, and declares what it does not
simulate (`GET /api/rules/supported-scope`):

- Executed by the engine: d20 ability, skill, save and tool checks against DCs of 10/15/20/25;
  weapon attacks with natural 1 and 20 handling and proficiency only where the class or sheet grants
  it (a versatile weapon deals its two-handed damage when the player declares both hands and no
  shield is carried); advantage and disadvantage from engine-owned sources (both d20 faces logged);
  AC from the armor table; initiative; combat rounds in initiative order; Fighter Second Wind (one
  use, a bonus action that may accompany the turn's action, restored by a short or long rest; no
  automatic use at 0 HP); Hit Dice tracked on the sheet (level dice of the class die, d6 to d12): a
  short rest spends the declared number of dice (one when none is declared, never more than are
  left), rolls them plus CON each and regains that total (capped at the missing HP), and is refused
  with none left; a long rest restores HP, spell slots and up to half the total Hit Dice, rounded
  down (PHB p.187; the 2024 campaigns use the same rule); healing potions taken from inventory. The
  AC table follows PHB p.146 (hide armor included), with barbarian and monk Unarmored Defense (PHB
  pp.49, 79). Armor the table marks with Stealth disadvantage gives disadvantage on Stealth checks;
  a heavy armor's Strength requirement only slows movement, which is not simulated, so it is not
  applied.
- Action economy in combat: each player turn has one action and one bonus action. Attacks, contested
  maneuvers, item use, catalog action spells, Dodge, Dash and Disengage use the action; Second Wind,
  Healing Word, Rage and the off-hand attack use the bonus action. A second action or bonus action in
  the same turn is refused with no roll and no round. Dodge gives attacks against the hero
  disadvantage, and the hero's Dexterity saving throws advantage, until the hero's next turn while
  the hero can act. Stunned, paralyzed or unconscious heroes receive neither Dodge benefit (the
  book's visibility condition is not applied). Ready (PHB p.194) takes the action and holds one melee
  attack for a named foe closing into melee; it is made with the hero's reaction when that happens,
  and the reaction comes back at the start of the hero's next turn (PHB p.191). The HUD and narration
  show what the turn spent, including the reaction.
- Two-weapon fighting (PHB p.195): a hero who takes the Attack action with a light melee weapon and
  has a different light melee weapon on the sheet (two copies of one weapon count) and no shield may
  declare an off-hand attack. It is a bonus action, refused with no roll when the bonus action is
  spent or no such pair is carried, and its damage adds no ability modifier unless that is negative
  (Rage's bonus still applies; Sneak Attack still adds its dice at most once per turn). The sheet
  does not say which hand holds a weapon, and throwing a weapon is not modelled.
- Declared actions (EDG-182): the parser proposes a combat turn as an ordered list of registered
  actions (attack, off-hand attack, maneuver, item, spell, Dodge, Dash, Disengage, Ready, Second Wind,
  Rage, move, contingency). The engine adopts a proposal only where its own reading of the player's
  words and state agrees; a disagreement, an unknown action, or a missing target among several foes
  is one question with no roll and no turn spent. A multi-part typed turn (two actions, or a move
  beside an action) is repeated back before any die: "yes" runs the stored declaration as proposed,
  "no" cancels, and any other reply is a new declaration. "always confirm" asks about every action
  turn for that hero (per encounter for now). A contingency ("if it drops, attack the other") is not
  executed yet: the turn is asked with the alternatives. Standing reaction orders, feet-based
  movement and Help are not modelled.
- Positioning in combat: each foe stands in the melee band (within 5 feet of the hero) or the ranged
  band (beyond it), set from its exact record (a foe whose listed attacks are all ranged starts at
  range). Movement is free and declared in the hero's words ("I close in on the archer", "I back
  away"): one band change per turn, two with the Dash action. A melee-only foe at range closes in on
  its turn and then attacks; a ranged-only foe stays at range. A melee attack or grapple cannot reach
  a foe at range and is refused with no roll, with the move that fixes it named. A ranged attack
  made within 5 feet has disadvantage. Leaving a foe's melee band without the Disengage action
  provokes one opportunity attack from that foe (PHB p.196), rolled by the engine with its reaction;
  a reaction is not taken again until the start of the creature's own next turn (PHB p.191). Exact
  distances, cover, flanking and high ground are not tracked.
- Class features in combat: Fighter Action Surge (one use, two from level 17, restored by a short or
  long rest; it adds one more attack on the same foe in that round), Rogue Sneak Attack (1d6 per two
  rogue levels, once per turn, on a hit with a finesse or ranged weapon that has advantage from an
  engine source) and Barbarian Rage (uses per long rest by level; +2, +3 or +4 damage on melee
  Strength hits and half damage from bludgeoning, piercing and slashing attacks, outside heavy
  armor, for 1 minute or until the encounter ends). Uses and recharge are tracked on the sheet; a
  declared feature the engine does not execute gets a ruling and no effect.
- Catalog spells, in combat and out of it: Fire Bolt (spell attack), Magic Missile (automatic-hit
  darts, upcast by slot), Sacred Flame (DEX save), Cure Wounds (self-heal, or another hero of a party fight) and Healing Word (bonus
  action, self-heal). Their dice are rolled by the engine, leveled spells spend a slot tracked per
  character, and a cast with no slot left is refused with no effect.
- Spell conditions in combat: Guiding Bolt (a spell attack for 4d6 radiant) marks a foe it hits, so
  the next attack roll against it has advantage; the mark is spent by that roll or ends at the end
  of the caster's next turn. Blindness/Deafness (a Constitution save against the spell save DC) blinds
  a foe that fails it, for one minute: the foe's own attacks have disadvantage, attacks against it
  have advantage, and it repeats the save at the end of each of its turns. Blindness only, at one
  foe; a declaration naming deafness is refused. The condition is stored on the foe, shown in the
  roster and ends with the foe's defeat or the encounter.
- Hold Person (concentration, EDG-043): a humanoid foe that fails its Wisdom save against the spell
  save DC is paralyzed for up to one minute. It cannot act but stays in the fight, attacks against it
  have advantage, a melee hit from within 5 feet is a critical hit (not from a reach weapon, whose
  distance is not tracked), and it repeats the save at the end of each of its turns. A foe whose
  record gives no readable humanoid type is refused before any roll or slot. Cast with a slot of 3rd
  level or higher it holds one more humanoid per level above 2nd, each with its own save and repeat
  save: the foes the player names first, then the next humanoids in roster order. The book's 30-foot
  limit between the targets is not modelled (the engine tracks no distances).
- Concentration: the caster holds one concentration spell at a time (Hold Person is the only
  catalog spell that needs it). Casting another ends the first
  and its effects on foes. Taking damage requires a Constitution save (DC 10 or half the damage,
  whichever is higher, rounded down, with the sheet's save modifier); a failure, or a fall to 0 HP,
  ends it, as does the spell's minute or the end of its effect on every foe. The spell is shown in the
  combat payload and the narrator's roster.
- Monster riders and recharge (EDG-043): an exact record whose attack reads exactly as "and the
  target must succeed on a DC N Constitution saving throw or be poisoned for 1 minute, ... repeat the
  saving throw at the end of each of its turns" poisons the hero on a hit after the engine-rolled
  save. The hero's attacks and ability checks then have disadvantage, and it repeats the save at the
  end of its own turn.
  An attack-style "Recharge X-6" attack is used once, then waits for a d6 at the start of the
  creature's turns. A hit that reads "If the target is a creature, it must succeed on a DC N Wisdom
  (or Constitution) saving throw or be frightened (or stunned) until the end of the <creature>'s next
  turn" is saved the same way and lasts until the end of that creature's next turn, with no repeat
  save. Any other rider wording stays declared.
- Monster breath weapons (EDG-043): an exact record's action that reads exactly as "exhales ... in a
  N-foot cone (or line). Each creature in that area must make a DC N ... saving throw, taking ... on
  a failed save, or half as much damage on a successful one" (or "must succeed on ... or take ...",
  no damage on a success) makes no attack roll. The hero rolls the saving throw with the sheet's
  modifier (plus proficiency when the class has that save), with advantage on a Dexterity save while
  dodging, and takes the engine-rolled damage on a failure and half of it, rounded down, on a
  success. A ready breath weapon replaces the Multiattack and uses the Recharge X-6 rule above. The
  hero is the only target, so the cone's shape and range are not modelled; other wording stays
  declared.
- Frightened and stunned hero (EDG-043): an exact record's Frightful Presence (a dragon's "Each
  creature of the dragon's choice ... must succeed on a DC N Wisdom saving throw or become frightened
  for 1 minute", with its repeat-save and 24-hour immunity sentences), made first as the Multiattack's
  opener once per fight (the book's 24-hour immunity is only tracked within that fight); a save action
  worded "... or take N (dice) <type> damage and be stunned for 1 minute" (the mind flayer's Mind
  Blast, "magically emits" in a cone); and the rider above leave a hero that fails the engine-rolled
  save frightened or stunned. A minute is 10 rounds, with the repeat save at the end of each of the
  hero's turns. A frightened hero has disadvantage on attack rolls, ability checks and Athletics
  contests while the source stands (line of sight is not modelled) and cannot close in on it. When the
  source is down, the condition and repeat save persist but those restrictions stop. Stunned
  incapacitates the hero (its turn passes with no action, move or flight), makes it fail Strength and
  Dexterity saves without a roll, gives attacks against it advantage, and ends concentration even
  when a damage concentration save succeeded. Conditions do not stack: a failed same-condition save
  from the same source refreshes that effect's timing; another source neither stacks nor replaces it.
  Stored source identity uses the stable combatant ID when available, otherwise the source name. A
  short fear or stun due to expire at an ahead source's turn is checked after that turn, so a
  reapplication still blocks closing in or acting. Any other wording stays declared.
- Combat maneuvers (PHB pp.196-197, Appendix A): in a combat round, against one foe in the melee band,
  in place of one attack. A grapple, a pin and a binding with rope are one maneuver: the hero's
  Athletics check is contested by the foe's Strength (Athletics) or Dexterity (Acrobatics), and the
  foe takes the better of its modifiers. A won check makes the foe grappled, whose speed is 0 and
  which takes no attack or save penalty (PDF p.291). A shove prone that wins knocks the foe prone: its
  attacks have disadvantage, and attacks against it have advantage from within 5 feet and
  disadvantage from farther away (PDF p.293). Restrained (PDF p.293), with its attack and Dexterity
  save penalties, is applied only from its other sources, never by a player's words. A declared
  release ends a grapple or restraint; a grapple ends with the foe's defeat or the encounter. A
  grappled foe does not escape, and the free hand and the size rule are not checked. These effects
  are shown in the combat roster and given to the narrator.
- Captives: a foe knocked out in a nonlethal victory stays alive as a captive. The player can release
  it (it is no longer a captive and is never an enemy again) or kill it, but only with an explicit
  lethal declaration in their own words; no dice are rolled. An attack on a captive without that
  declaration starts no combat and receives a ruling asking whether to release, question or kill.
  Released, killed and captured foes are recorded in the session story ledger.
- Not supported: advantage claimed by the player or model without an engine source, shove away and
  other maneuvers, escape contests, spells outside that catalog, other concentration spells, other
  conditions (charmed and the rest have no engine effect; frightened and stunned only from the exact
  wordings above), area effects on more than the hero and save actions whose wording is not read
  exactly, cover, high ground, flanking, difficult terrain and exact distances, Help and Hide (no
  Help between heroes of a party fight; no hidden state), reactions other than opportunity attacks and a readied melee
  attack, readied triggers other than a foe closing into melee, readied ranged attacks, spells and
  moves, other bonus actions, and class features other than
  Second Wind, Action Surge, Sneak Attack and Rage (Divine Smite, Lay on Hands, Bardic Inspiration,
  Channel Divinity, Wild Shape, sorcery and ki points, Flurry of Blows, Stunning Strike,
  Superiority Dice, Martial Arts; Rage's Strength advantage and early end). A declared action of
  this kind gets a narrative ruling and no mechanical effect.
- Declared-action reading (EDG-147, measured against real model outputs; see
  `tests/fixtures/real_model/README.md`): Declared actions are read in English. Italian and Spanish are read only for a few casting, advantage, cover, lethality and action words. A catalog spell resolves only when its English name is in the declaration; a spell the parser names from another language is ruled as not resolved. Other languages are not classified by these words and rely on the parser's own entities. The unsupported-action
  classifier, the lethality reading and the narration checks were re-measured on 120 labelled
  player inputs (used for tuning) and on 30 held-out inputs. The held-out set was labelled before
  its model run, but two of its misses were then fixed, so it is no longer a clean test. Remaining
  limits: a parser-declared nonlethal with no word cue ("tackle the goblin to the ground") is read
  as a nonlethal intent; a parser-named catalog spell with no English name is ruled as not resolved;
  the narrator revived no dead or unconscious foe in 39 real samples, including when asked to.
- Exact monster records (EDG-004): a creature identified by its verified record uses that record's
  armor class, hit points, DEX initiative bonus, saving-throw bonuses (for a spell's save), attacks
  and traits. Each listed attack is rolled by the engine; a stated Multiattack makes one roll per
  listed attack. Regeneration heals at the start of the monster's turn, unless the damage types the
  record names have been taken since its last turn. Resistances, immunities and vulnerabilities apply
  to player damage by weapon or spell damage type. A legendary creature can spend at most one
  legendary action at the end of each other creature's turn represented in encounter order, choosing
  the most expensive affordable option, and regains them at the start of its own turn; only an option
  that names one of its own plain attacks and the dragon Wing Attack
  (damage, prone on a failed save, and flight into the ranged band) are executed. Reviewed actor
  setup may explicitly mark the actor or hero surprised through its first turn; actions, movement,
  reactions and legendary/lair use obey that flag. An actor's `lair_reference` binds an unlocked
  private planner passage to the same creature as its `stat_reference`. Exact red-dragon magma
  and tremor options execute at initiative 20, losing every tie, without consecutive repetition
  after reload. Persistent gas/clouds, darkness, terrain geometry and other lair options remain
  unsupported. Listed monster spells resolve
  only when their exact spell name and level are in the small supported catalog; recorded slots are
  spent by level, while unsupported list entries are counted generically and never executed. The
  supported effects include spell attacks and saves, healing, Guiding Bolt's mark, and Hold Person's
  concentration and repeat saves. Exact speed and movement, unsupported lair and reaction actions,
  other legendary options, unsupported spells and save actions,
  recharge abilities not read exactly, a Multiattack's later-attack disadvantage, other rider wording
  and unlisted traits are declared in the combat start message and have no effect. Standing up from
  prone clears the condition without an action; the movement-band abstraction does not charge the book's
  half-speed cost. Regeneration's 0 HP clause is declared too: an exact creature at 0 HP is defeated
  at once. Stat blocks were checked against real Monster Manual layouts (see
  docs/rules-verification-2014.md).
- Campaign modes (EDG-045), stored per campaign and shown in its settings. Progression is `fixed`
  (the default: the sheet keeps its level) or `milestone`: each won encounter grants one milestone, and
  an explicit level-up spends it to reach level 5 for fighter, rogue, wizard, cleric and ranger. A
  level-up adds the class table's fixed hit points (or an engine roll of the die) plus CON, follows the
  proficiency bonus, Hit Dice and spell slot tables (PHB pp.58, 72, 91, 96, 114), and offers the
  Ability Score Improvement at level 4 (PHB p.73). Class features the engine does not run are named,
  not applied. Death is `subdued` (the default: a defeat leaves the hero unconscious at 1 HP) or
  `death_saves`: the PHB death saving throws, instant death from massive damage, and permanent death,
  recorded in the story ledger, with a replacement hero as the way on (PHB pp.198-199).
- Solo adaptations: in the default subdued mode defeat leaves the hero subdued at 1 HP rather than dead, and encounters are
  fitted to a single hero by an engine-enforced budget (`apps/agent/src/agent/encounter_budget.py`) that scales
  with level. Its limits are checked against the 2014 DMG XP thresholds (`apps/agent/src/agent/dmg_thresholds.py`):
  the worst generic roster at every level 1-20 stays at or below that hero's deadly threshold.
  Foes without an exact record are clamped or left out. An exact creature is never rescaled: the budget
  limits only how many foes take part, and an out-of-band record is noted as unscaled. An exact foe above a
  solo hero's deadly threshold gets an engine-written warning (above deadly, or at least twice deadly) in the
  combat start and the combat state; the fight is not refused. The combat start notes any adaptation.

## Repo layout

```
Edgar/
  frontend/          React + Vite + Tailwind SPA (npm); served at /ui by the API
  apps/
    api/             FastAPI: routers, schemas, services; bundles frontend/dist in Docker
    agent/           LangGraph DM agent (graph, nodes, models, prompts)
    ingestion/       PDF -> chunks -> embeddings -> Qdrant
    rules_engine/    Rules-only RAG demo (CLI)
  core/              edgar_core: config and env loading
  db/
    postgres/        SQLAlchemy 2.0 async models, session, base
    alembic/         Migrations
    vector/          Qdrant client, embeddings, rules vs adventure retrieval
  tools/             Dice tool (deterministic, used by the agent)
  data/              PDFs and extracted markdown (gitignored, bind-mounted)
  work-register/     Optional local-only register, individual work briefs and audit evidence
  tests/             Pytest suite (testcontainers Postgres, fakeredis, fake LLM)
  Dockerfile         Multi-stage: python-base / migrate / api / ingestion / rules_engine
  docker-compose.yml api + db + redis + qdrant; migrate/ingestion/rules_engine behind profiles
  docker-compose.dev.yml  Opt-in backend hot reload and current migration source
  pyproject.toml     uv workspace root
  Makefile           build / up / down / logs / migrate / dev-* / seed / ingest / rules / test / lint / check-migrations / frontend-*
```

`db/`, `tools/`, `core/`, and the four apps are independent uv workspace members; the workspace root lockfile is the only `uv.lock` you should regenerate.

The optional personal [work register](work-register/REGISTER.md) is the local planning entry point: choose an Open item, follow its linked Markdown brief, then ask the implementing agent to start by ID. Each brief includes scope, acceptance criteria, dependencies and preserved audit evidence. The register, story library and copied reports/playtest evidence are gitignored, excluded from Docker build contexts and absent from a fresh clone. Repository setup and operation do not require them. The superseded root `audit/` folder was removed after the independent coverage review passed; the library retains its evidence independently. The intentionally deleted old database dump and ingestion logs were not recreated.

## Configuration

`config.py` lives in `core/src/edgar_core/config.py`. It reads component env vars (`DB_HOST`, `POSTGRES_USER`, etc.) and builds the URLs the rest of the code consumes (`DATABASE_URL`, `REDIS_URL`, `VECTOR_DB_URL`). Docker overrides only host names so the same `.env` works on the host (via `localhost`) and inside Compose (via `db`, `redis`, `qdrant`). `LLM_MODEL` and `EMBEDDING_MODEL` are env-overridable; defaults are `gpt-6-luna` and `text-embedding-3-small`. Reasoning models (`gpt-5*`, `gpt-6*`, `o*`) only accept the default temperature, so the agent sends `LLM_REASONING_EFFORT` (default `low`) instead and calls them through the OpenAI Responses API (Chat Completions rejects function-calling structured output combined with a reasoning effort on these models); classic models such as `gpt-4o-mini` still get per-node temperatures.

The API sends structlog, standard-library/server logs and in-process ingestion logs through one output formatter. `ENV=local` uses readable console output; other values use JSON with UTC timestamps, log level, logger name and request correlation. `X-Request-ID` accepts bounded IDs from a proxy and is echoed on responses, including unhandled errors. Common credential fields and credential URL forms are redacted, SQL bound parameters are hidden, and traceback locals are disabled. Avoid logging request bodies, prompts or arbitrary secrets: pattern-based redaction is not a complete privacy policy. Provider and ingestion CLI failures log `error_summary` (`edgar_core.log_safety`: exception class, HTTP status, provider request id), never the exception message, which can echo prompts or player text. The ingestion CLI sink applies the same credential redaction.

## Tests

```
make test
```

The suite spins up Postgres 16 via testcontainers (running `alembic upgrade head`), fakes Redis with `fakeredis`, fakes the OpenAI LLM with a deterministic stub, and runs the full turn flow end to end. Docker must be reachable: locally the DB-backed tests are skipped without it, but with `CI` set they fail instead. Tests never use a real OpenAI key (`OPENAI_API_KEY` defaults to a dummy value).

After changing a model, add an Alembic migration in `db/alembic/versions/` and confirm `make check-migrations` (`alembic check` against your migrated database) reports no drift.

Revision `11_character_bounds` enforces the public character bounds in PostgreSQL (levels 1–20, positive maximum HP and nonnegative assignment HP) and adds an index for session/event-type/id reads. It checks existing rows before making changes and refuses invalid data with counts and constraint names. Correct any invalid historical rows explicitly before retrying; this migration does not clamp or delete them. Run migration upgrade/downgrade verification on a disposable database, then use `make migrate` or `make dev-migrate` for the selected running configuration.

## CI

`.github/workflows/ci.yml` runs on pushes and PRs to `main`:

* **edgar-ruff**: `uv sync --locked --group dev`, then `ruff check .` (rules `E,F,I,B,UP`; `audit/`, `plans/` and `frontend/` are excluded). Formatting is not enforced yet.
* **edgar-tests**: the pytest suite with `CI=true` (missing Docker fails the job).
* **edgar-migrations**: against a Postgres service container, `alembic upgrade head`, `alembic check` (models vs migrations drift), and a downgrade/upgrade of the latest revision.
* **docker-build**: builds the `api` and `migrate` Docker targets.
* **stack-smoke**: validates the base and dev Compose configs, then starts a disposable project (unique `COMPOSE_PROJECT_NAME`) with a dummy provider key: migrations, `/ready` (Postgres, Redis, Qdrant), the bundled `/ui/`, `/health` with `X-Request-ID` echo, and a seed-then-read flow. Logs are dumped on failure and `down -v` always runs.
* **frontend**: `npm ci`, `npm run lint`, `npm test --if-present`, `npm run build`.

CI pins uv to the version that writes `uv.lock`; regenerate the lock with that version (`uv lock`).
