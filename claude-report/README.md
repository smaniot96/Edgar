# Edgar — Production Readiness Review & Solo Campaign Playthrough

**Author:** Senior engineering review (autonomous)
**Date:** 2026-06-18
**Scope:** Prime the codebase, deep-scan for problems, run the app under Docker locally, play and **finish a full solo campaign** as a real user, fix what blocked or degraded the experience, and produce a multiplayer roadmap.

---

## 1. Executive summary

Edgar is a solo AI Dungeon Master: a FastAPI + LangGraph backend that runs a D&D 5e adventure, with Postgres as the source of truth, Qdrant for rulebook/adventure RAG, Redis for a per-session turn lock, and a React chat UI. The architecture is genuinely good — clean separation between *the LLM proposes* and *the rules engine/dice/DB decide*, transactional world-writes, a config seam, structured logging, and a real testcontainers suite.

**But on arrival the product could not deliver its core promise.** The campaign was effectively *unplayable as authored and unfinishable*:

1. **RAG was 100% dead** — a silent `qdrant-client` API break meant **every** retrieval returned zero results. The DM improvised generic fantasy and ignored the actual adventure (wrong tavern, wrong NPCs, wrong plot). This is the single most damaging bug; it was invisible because the failure was swallowed by a bare `except`.
2. **Combat was unplayable and unwinnable** — only one actor resolved per HTTP turn, the player's typed action was silently consumed by enemy turns if they lost initiative, enemies had **no HP** (so they could never die), and combat could only end via a 10-round cap or a fragile `"combat ends"` substring match. Boss fights were impossible.
3. **There was no way to *finish*** — no completion/victory mechanic existed at all.

After a focused fix cycle (details below), I **played the default adventure end-to-end as a solo player and finished it** — 21 turns from arriving in Greywood to defeating the temple paladin Helena and exposing the false god, with the campaign auto-marked complete. Full transcript: [`artifacts/campaign-transcript.md`](artifacts/campaign-transcript.md).

**Current state:** the solo experience is now good — grounded, immersive storytelling; tense, winnable, balanced combat; persistent HP and world state; and a satisfying, automatic ending. Remaining work for true production (auth, rate-limiting/cost controls, key rotation, CI hardening, broader tests) is itemized in §6 and is **required before any public/multi-user deployment**.

---

## 2. What I did

1. **Primed** on the full codebase (architecture in §3).
2. **Deep-scanned** with four parallel specialist reviews (security, backend correctness, frontend/UX, platform/SRE) — raw reports in [`artifacts/`](artifacts/).
3. **Brought the stack up under Docker** (Postgres, Redis, Qdrant, API), ran migrations, seeded, and **ingested the four PDFs** (PHB, DMG, MM, the Chalice adventure) into Qdrant.
4. **Played the game as a user** through the real API, hit the bugs live, and ran a **fix → re-play feedback loop**, using background subagents for well-scoped, non-overlapping fixes while I drove the combat/agent redesign and the live playthrough.
5. **Finished the campaign** and verified the end state (campaign `ended`, further turns gated with 409).
6. Authored this report and the **multiplayer plan** ([`multiplayer-plan.md`](multiplayer-plan.md)).

---

## 3. Architecture (as built)

```
Player → React SPA (/ui) → FastAPI
                              │  POST /api/sessions/{id}/turn          (sync JSON)
                              │  POST /api/sessions/{id}/turn/stream    (SSE)
                              ▼
        Redis turn-lock (per session, NX+TTL)
                              ▼
        LangGraph agent  (apps/agent)
          input_parser → [conditional] → world_retriever → rules_adjudicator
                          │                → world_state_updater → narrator → memory_summarizer
                          └─ combat ─→ combat subgraph
                              ▼
        Postgres (source of truth)   Qdrant (RAG: rules_* + chalice_*)   OpenAI (gpt-4o-mini, text-embedding-3-small)
```

- **LLM proposes, tools decide:** the adjudicator emits a `dice_expression`; `tools/dice.py` rolls it authoritatively; the DB clamps and persists HP/flags/scene/inventory.
- **RAG split:** the *adjudicator* only sees rules excerpts (PHB/DMG/MM); the *narrator* additionally sees adventure-module text. Good separation of mechanics vs. story.
- **LLM calls/turn:** ~3 for RP/exploration (parse + adjudicate + narrate; +1 memory summary every ~10 messages). Combat now ~3–4 (parse + encounter build on first round + adjudicate player action + narrate) — enemy turns are **deterministic dice, no per-enemy LLM call** (an improvement over the previous per-enemy-call design).

---

## 4. Findings (consolidated, by severity)

Full detail in [`artifacts/backend-findings.md`](artifacts/backend-findings.md), [`security-findings.md`](artifacts/security-findings.md), [`frontend-findings.md`](artifacts/frontend-findings.md), [`platform-findings.md`](artifacts/platform-findings.md).

### CRITICAL — blocked playing/finishing (all FIXED)
| # | Finding | Root cause |
|---|---------|-----------|
| C1 | **RAG returns nothing; DM ignores the adventure** | `qdrant-client>=1.17` removed `Client.search()`; `db/vector/retrieval.py` called it inside `except Exception: continue`, silently returning `[]` for every query. |
| C2 | **Player's action consumed by enemy turns** | Combat resolved one actor per HTTP turn; if the player lost initiative, their typed message drove an enemy's slot and was discarded. |
| C3 | **Enemies have no HP; combat unwinnable** | `combat_state` stored only names/round/index; nothing tracked or reduced enemy HP, and only the player's HP was ever written. |
| C4 | **No campaign completion path** | Only a manual, reopenable `ended` status existed; nothing detected or produced an ending. |

### HIGH (FIXED or mitigated)
- **Combat end was a substring match** on LLM free text (`"combat ends"`) → fixed with deterministic victory/defeat/flee/stalemate.
- **Combat stalled if a mid-fight line wasn't classified `combat`** → routing now stays in combat until the encounter resolves.
- **Dice tool 503'd the whole turn** on `"d20"`, `"2d6 + 3"`, `"1D20"` → parser hardened to accept them.
- **Internal exception text leaked** to clients on the sync turn endpoint → replaced with a generic message + server-side log.
- **OpenAI calls had no timeout/retry** → added `timeout=60`, `max_retries=3`.
- **`.env` baked into image layers** (real OpenAI key) → `.env` added to `.dockerignore`. **The previously-baked key must be rotated.**
- **Unbounded turn message** → capped at 2000 chars.

### Known-remaining (documented, see §6)
- **No authentication/authorization** (single hard-coded user; any client can mutate any campaign). **Blocker for multi-user.**
- **No rate limiting / per-user spend cap** (financial-DoS exposure if public).
- **No CORS policy**, datastores exposed with default creds, containers run as root.
- **Combat/SSE have thin automated test coverage**; CI doesn't build the Docker image or scan deps.
- `world_state_updater_node` writes telemetry in its own transaction (can diverge from a rolled-back turn).

---

## 5. Fixes applied (this engagement)

Code changes live in the repo (branch `dev`); per-area fix notes in [`fixes/`](fixes/).

**Gameplay (the reason it now works):**
- `db/vector/retrieval.py` — use `query_points` (modern qdrant API) with a legacy `search` fallback; attach scores. **This single fix restored all adventure/rules grounding.**
- `apps/agent/src/agent/graph_combat.py` — **rewritten**: one full round per HTTP turn (player acts, then every *living* enemy acts), per-combatant HP tracked in the DB, a grounded encounter-builder that scales enemies for a solo level-1 hero and excludes allies, deterministic victory/defeat/flee/stalemate, Fighter **Second Wind**, and a consolidated single-narration round.
- `apps/agent/src/agent/graph.py` — routing stays in combat until the encounter ends.
- `db/postgres/models/combat_states.py` + migration `9_combat_combatants.py` — `combatants` (JSONB) and `outcome` columns.
- `apps/api/services/combat.py` — load/persist the new combat fields.
- `apps/api/services/campaign_completion.py` (new) + `prompts.py` + both turn paths — auto-end the campaign when the adjudicator flags `campaign_complete`, with an epilogue.
- `tools/dice.py` — lenient parsing (`d20`, `2d6 + 3`, `1D20`).
- `apps/agent/src/agent/state.py` — declare `npcs` (was silently dropped).

**Solo-play design choices (deliberate, noted for review):**
- **Defeat = subdued, not dead.** With no human GM to adjudicate death, a wipe leaves the hero at 1 HP / captured so the story can continue (this matches the adventure, where the party is meant to be arrested). Documented; tune for harder modes later.
- **Encounters auto-scale down for a solo level-1 hero** (a lone boss ≈ 18–30 HP). Without this a solo PC cannot survive a party-sized boss.
- **Fighter Second Wind** gives a realistic once-per-fight rally so boss fights are tense-but-winnable rather than instant losses.

**Production hardening (partial):** OpenAI timeout/retry, `.env` out of the image, message length cap, error redaction (see §6 for what's still required).

**Reliability:** full existing test suite still passes (**30/30**). The dev stack runs via a `docker-compose.override.yml` that bind-mounts source for fast iteration; the production image builds from the unchanged `Dockerfile`.

---

## 6. Production readiness checklist (remaining)

Ordered by priority. None of these block *solo local play*; all matter before exposing the service.

1. **Rotate the OpenAI key** (it was baked into prior image layers) — then keep `.env` out of images (done).
2. **Authentication + authorization** — replace the single-user `current_user_id()` seam with real auth; scope every campaign/session/character route to the owner. (Prereq for multiplayer — see plan.)
3. **Rate limiting + per-user spend cap** on the turn endpoints (each turn costs OpenAI calls).
4. **CORS policy**, non-root containers, datastore credentials/secrets, restart policies, an API healthcheck, and a `/ready` that checks Redis + Qdrant (not just Postgres).
5. **Tests for combat + SSE + completion** and a fake LLM that can drive a combat round; **build the Docker image in CI** and add dependency/secret scanning.
6. **Cost/consistency cleanups**: avoid the redundant input-parser call on combat turns; fold `world_state_updater` telemetry into the request transaction; add metrics/OTel.

---

## 7. How to reproduce

```bash
cp .env.example .env            # set a valid OPENAI_API_KEY
docker compose up -d db redis qdrant
docker exec dnd-api ... # or: docker compose run --rm migrate   (after building images)
docker compose up -d api
curl -fsS -X POST localhost:8000/api/seed

# Ingest the corpus into Qdrant (PDFs in data/pdfs/):
docker compose run --rm ingestion /data/chalice_of_the_mountain_god.pdf --collection chalice_of_the_mountain_god
docker compose run --rm ingestion /data/player_handbook.pdf --collection rules_player_handbook
docker compose run --rm ingestion /data/dm_guide.pdf        --collection rules_dm_guide
docker compose run --rm ingestion /data/monster_manual.pdf  --collection rules_monster_manual

# Play (UI): open http://localhost:8000/ui
# Play (API/script): python3 claude-report/play_harness.py "I look around Greywood."
```

> Note: the migrate/api **images bake the migrations** (`COPY . .`). I rebuilt both images at the end of this engagement, so `docker compose run --rm migrate` now upgrades to head `9_combat_combatants`. (During iteration I applied migration 9 via the source-mounted `api` container; that is no longer necessary.)

---

## 8. Deliverables in this folder

- `README.md` — this report.
- `multiplayer-plan.md` — roadmap to multiple players at one table.
- `artifacts/` — raw deep-scan reports, the full campaign transcript, and the play log.
- `fixes/` — per-area change notes.
- `play_harness.py` — the script I used to play through the API.
- `logs/transcript.jsonl` — machine-readable turn-by-turn log of the finished campaign.
