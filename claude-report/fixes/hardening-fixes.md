# Production-Hardening Fixes

Date: 2026-06-18

## 1. OpenAI reliability — timeout & bounded retries
**File:** `apps/agent/src/agent/llm.py:7-23`

`make_chat_model` previously built `ChatOpenAI` with no `timeout` or
`max_retries`, so a hung or rate-limited OpenAI call could pin a turn
indefinitely. Added a 60s request `timeout` and `max_retries=3`, both passed
through to `ChatOpenAI`. Exposed them as new optional params
(`timeout=60`, `max_retries=3`) so callers can override; the existing
`make_chat_model(temperature=0)` signature is preserved. Added a docstring
explaining the bounds.

## 2. Secret leak — `.env` baked into image
**File:** `.dockerignore:14-17`

The Dockerfile does `COPY . .` and `.env` was not ignored, so the real
OpenAI key was baked into image layers. Added `.env` and `.env.*` to
`.dockerignore`, with `!.env.example` retained so the example template still
ships. Dockerfile was not modified.

**ACTION REQUIRED — ROTATE THE KEY:** the OpenAI API key was previously
committed into Docker image layers. Excluding it going forward does not
remove it from existing/published image history. The leaked key must be
rotated (revoke old, issue new) as it should be considered compromised.

## 3. Unbounded input — `TurnRequest.message`
**File:** `apps/api/schemas/turn.py:9`

`TurnRequest.message` already required `min_length=1` but had no upper bound,
a DoS / token-cost vector. Added `max_length=2000`. Existing behavior is
otherwise unchanged.

## Verification
Ran in the running container:

```
docker exec dnd-api uv run python -c \
  "from agent.llm import make_chat_model; from apps.api.schemas.turn import TurnRequest; print('ok')"
```

Output: `ok` (only a pre-existing, unrelated LangChain deprecation warning).
