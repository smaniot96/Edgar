# Authentication and ownership (EDG-065)

Edgar runs in one of two auth modes, chosen by `EDGAR_AUTH_MODE`. The default is the single-user local
app, which is unchanged. The hosting mode adds bearer-token authentication, ownership checks on
campaign data and operator-only routes. Code: `apps/api/auth.py`; route wiring: `apps/api/main.py`
and the routers; tests: `tests/test_auth_*.py`, `tests/test_route_auth_matrix.py`,
`tests/api/test_auth_token_mode.py`.

## Modes and settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `EDGAR_AUTH_MODE` | `local` | `local`: no authentication, the first user is the caller (the original behaviour). `token`: every `/api` route requires `Authorization: Bearer <token>`. Any other value is rejected at startup. |
| `EDGAR_ADMIN_USER_IDS` | unset | Comma-separated user ids that are operators. Required in token mode. Ignored in local mode. |
| `EDGAR_ALLOW_DEBUG_IN_TOKEN_MODE` | unset | Set to `1` to allow `EDGAR_DEBUG` in token mode. Without it, token mode refuses to start while `EDGAR_DEBUG` is on. |

Existing settings still apply in token mode: `EDGAR_DEBUG` gates developer diagnostics, and
`EDGAR_METRICS_ENABLED` gates `/metrics`.

### Startup validation

`validate_auth_configuration()` runs first in the API lifespan (the same pattern as the provider
check) and fails before any background work starts. Messages name the setting, never its value.

- An unknown `EDGAR_AUTH_MODE` is refused.
- Token mode without `EDGAR_ADMIN_USER_IDS` is refused.
- Malformed admin ids (non-numeric, zero, negative, empty entries) are refused.
- Token mode with `EDGAR_DEBUG` on is refused unless `EDGAR_ALLOW_DEBUG_IN_TOKEN_MODE=1`.

Request-time mode resolution fails closed: any value other than an empty, `local` value is treated as
token mode.

## Identity

- `require_authentication` is the single guard. In local mode it returns `None` and does not query
  the database. In token mode it reads the bearer token and returns a `Principal`
  (`user_id`, `token_id`, `is_admin`).
- `current_user_id` (in `apps/api/dependencies.py`) returns the principal's user id. In local mode it
  is the first user, or the original 400 when no user exists.
- `principal_for(request)` returns the principal recorded for the request. EDG-066 rate limits can key
  on `principal.user_id` through this hook. The limits module itself is not changed here.

## Token lifecycle

- Tokens are `edgar_` followed by 256 bits of `secrets.token_urlsafe` output.
- The `api_tokens` table (migration `17_api_tokens`) stores only the SHA-256 digest (`token_hash`,
  unique), the user, a label, `created_at`, `revoked_at` and `last_used_at`. The plaintext is never
  stored, logged or queried.
- Lookup is by digest, then a constant-time re-compare. Revoked and unknown tokens produce the same
  generic 401 (`authentication_required` in the safe-error taxonomy).
- `last_used_at` is written at most once every five minutes.
- SHA-256 without a server-side pepper is deliberate. The token is 256 bits of randomness, not a
  password, so brute force against the digest is not feasible. A pepper would add a secret to rotate
  without improving that.
- No expiry is implemented. Rotate by creating a new token and revoking the old one.

Operator commands (run from the repo root with the project environment):

```sh
.venv/bin/python -m tools.manage_tokens create --user-id 1 --label laptop   # prints the token once
.venv/bin/python -m tools.manage_tokens list [--user-id 1]                  # metadata only
.venv/bin/python -m tools.manage_tokens revoke --token-id 3                 # idempotent
```

There is no HTTP self-service signup, password flow or token-management endpoint in this change.

## Admin

Admin is an allow-list of user ids in `EDGAR_ADMIN_USER_IDS`, not a database flag. This keeps admin
rights under operator control in the environment, with no schema change, and nothing a user can
grant through the API. Admin is an operator role. It is not a superuser over campaign data: the
diagnostics routes are the one exception, because they are operator tooling that reads any session
(see the matrix).

## Ownership

Campaign data has one owner, `campaigns.created_by`. Sessions, campaign characters, NPCs and world
flags are reached through their campaign. Characters are owned by `characters.owner_user_id`.

- Owned lookups (`owned_campaign`, `owned_session`, `owned_npc`, and `authorize_campaign` /
  `authorize_session` before the turn lock) add the owner filter in token mode. A resource that
  belongs to someone else returns the same 404 as a missing one, so existence does not leak.
- Lists (`/campaigns`, `/sessions`, `/characters`) return only the caller's rows.
- The turn and intro endpoints check ownership before they take the per-session lock, so another
  user cannot hold a session's lock.
- In local mode the helpers do not filter. The queries and 404 messages match the previous
  `get_or_404` calls.

There is no membership table. `campaign_members` belongs to the multiplayer work (EDG-046). Until
then, a campaign is visible only to its creator.

## Protected-route matrix

`tests/test_route_auth_matrix.py` enforces this matrix. It fails if a route is neither on the public
list nor guarded, and if the admin set changes without an update here.

| Class | Routes | Token-mode rule |
| --- | --- | --- |
| Public | `/health`, `/ready` (component statuses only), `/`, `/docs`, `/redoc`, `/openapi.json`, `/ui/*` (static UI) | No credential needed. |
| Authenticated, shared read | `GET /api/adventures`, `GET /api/adventures/{slug}`, `GET /api/rules/supported-scope`, `GET /api/character-presets`, `GET /api/users/me` (own user), `PATCH /api/users/me` (own user), `POST /api/adventures/{slug}/campaign` (creates a campaign owned by the caller) | Any valid token. Private preparations are hidden from non-admins. |
| Owner-scoped | `/api/campaigns*` (including `/characters`, `/npcs`, `/world-flags`, `/sources/{slug}/upload`), `/api/sessions*` (including `/turn`, `/turn/stream`, `/intro`, `/turns/{id}`, `/combat`, `/messages`, `/map*`, `/assets/{id}`, `/journal*`), `/api/characters*`, `/api/npcs/{id}` | The caller must own the campaign, session, character or NPC. Others get 404. |
| Admin only | `POST /api/seed` (attaches to the caller's own account), `POST /api/users` (creates accounts, with no first-user gate in token mode), `GET /metrics`, `GET /api/sessions/{id}/diagnostics/{combat,turns,turn-traces}`, every adventure catalogue write: `POST /api/adventures/{upload,generate}`, `POST /api/adventures/{slug}/{retry,cancel,preparation/setup}`, `PATCH`/`DELETE /api/adventures/{slug}`, the manifest routes `/api/adventures/{slug}/manifest*`, extraction previews `GET /api/adventures/{slug}/extraction*`, and source recovery `POST /api/campaigns/{id}/sources/{slug}/upload` (which also requires campaign ownership) | Admin token required (403 otherwise). The diagnostics routes stay behind `EDGAR_DEBUG` (404 when off). |

Failures use the safe-error taxonomy: `authentication_required` (401, with `WWW-Authenticate: Bearer`)
and `forbidden` (403) are new entries. Their messages never include internals.

### Behaviour changes that apply in both modes

- A character owned by another user is now reported as `404 Character not found` in campaign roster
  assign, patch and release. It was `403`, which revealed that the character existed. Single-user
  local data is unaffected.

## Hosting checklist

The steps below are required before exposing the API beyond localhost.

1. Set `EDGAR_AUTH_MODE=token` and `EDGAR_ADMIN_USER_IDS` to the operator's user id(s). Confirm that
   `EDGAR_DEBUG` is off.
2. Apply migrations to head (`17_api_tokens`) in the same release.
3. Create one token per operator and per client with `tools.manage_tokens`. Store the token in the
   client's secret store. Never commit it.
4. Keep the Compose port binding on `127.0.0.1` or put a TLS-terminating reverse proxy in front of the
   API. This change does not add TLS.
5. Scrape `/metrics` with an admin token, or keep `EDGAR_METRICS_ENABLED` off.
6. Run the protected-route matrix test and the token-mode API tests in CI (they run with Docker).

## Not done in this change

- No UI login. The bundled frontend sends no `Authorization` header, so it cannot run in token mode.
  Hosting therefore needs a client change first. Until then, a token can be used with the API directly.
- No password, OAuth or SSO flow, and no signup or self-service token endpoint.
- No multiplayer membership (`campaign_members`, invites, seats). That is EDG-046.
- Rate limits and spend caps are not keyed per user here. EDG-066 owns that and can use `principal_for`.
- No token expiry, rotation schedule, or audit log of token use. `last_used_at` is the only record.
- No TLS.
- Adventure-catalogue writes are operator-only. Users cannot upload or generate their own adventures
  in token mode. Per-user adventures would need an owner column on adventure metadata.
