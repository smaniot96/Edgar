# Cross-origin hosting (EDG-052)

Edgar's API and UI are same-origin by default. This page covers the one supported alternative, a
separately hosted client, and states plainly what the cross-origin policy does and does not protect.

## Supported topologies

1. **Same-origin (default, no configuration).** The API serves the built UI at `/ui` and the JSON and
   SSE routes at `/api` on one origin. The Vite dev server proxies `/api` to the API. The API sends no
   CORS headers, exactly as before this change.
2. **Separately hosted UI with an explicit allow-list.** The UI is served from its own origin (for
   example `https://ui.example.com`) and calls the API with `apiBase` set to the API's public URL.
   Set `EDGAR_ALLOWED_ORIGINS` to that exact origin. Nothing else is granted.

Cookie-based sessions are not supported. The bundled client sends no cookies and reads no
credentials, so `EDGAR_ALLOW_CREDENTIALS` is off and should stay off until a cookie-auth design exists.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `EDGAR_ALLOWED_ORIGINS` | empty (same-origin only, no CORS headers) | Comma-separated exact origins, for example `https://ui.example.com,http://localhost:5173`. |
| `EDGAR_ALLOW_CREDENTIALS` | `0` | `1` adds `Access-Control-Allow-Credentials: true`. Requires `EDGAR_ALLOWED_ORIGINS`. Not needed for bearer-token mode (see below). |
| `EDGAR_TRUSTED_HOSTS` | empty (no host check) | Comma-separated bare host names that the `Host` header must match, for example `edgar.example.com,localhost`. |

Validation runs when the API module is imported, so a bad value stops the process before it serves a
request. The error names the setting only and never echoes its value.

- An origin must be `http://` or `https://`, with host and optional port, and no path, query, fragment
  or credentials. A default port (`:80` for http, `:443` for https) is dropped, because browsers omit
  it. IPv6 origins use bracket form, for example `http://[::1]:5173`.
- `*` is rejected, including with credentials. The opaque `null` origin is rejected.
- Trusted hosts are bare DNS names or IPv4 addresses. Wildcards, schemes, ports and IPv6 literals are
  rejected. `EDGAR_TRUSTED_HOSTS` also checks the `Host` header of every request, including
  `/health`. Include `localhost` if a local probe or proxy uses it.

When `EDGAR_ALLOWED_ORIGINS` is set, the policy is:

- Exact-origin echo. The response carries `Access-Control-Allow-Origin` equal to the request origin, and
  only for origins on the list. `Vary: Origin` is always sent when the policy is active.
- Methods: `GET`, `POST`, `DELETE`. These are the methods the bundled client uses.
- Request headers: `Authorization`, `Content-Type`, `Accept`, `X-Request-ID`.
- Exposed response header: `X-Request-ID`. The bundled client reads it. `Retry-After` is not exposed
  because the bundled client does not read it.
- Preflight cache: 600 seconds, a fixed constant.

## Why there are no CSRF tokens here

Browsers attach cookies to cross-site requests. A cross-site page cannot set an `Authorization` header on
a simple request, and cannot read a response that CORS has not allowed.

- **Current code (local mode, no authentication).** There are no cookies and no credentials. CORS only
  decides whether a page may read a response. It does not stop a request from reaching the server.
  See the request-mode findings below.
- **Bearer-token mode (EDG-065; bundled UI in EDG-160).** The token is sent in the `Authorization` header
  by the client code. A browser does not add it ambiently. A cross-site form or simple request therefore
  carries no credential and fails authentication. A separately hosted client then needs only the origin
  allow-list. `EDGAR_ALLOW_CREDENTIALS` stays off. `tests/api/test_token_mode_cross_site.py` proves the nine
  routes in the table below return 401 and reach no handler in token mode without the header.

Cookie sessions would change this. They would need SameSite cookies, Origin checks on unsafe methods,
or CSRF tokens. None of those is implemented.

## Request-mode findings (local mode, observed)

Measured on the current base with the real app, using ASGI requests and stubbed dependencies
(`tests/api/test_cross_origin_requests.py`). Each entry is a fact about the server, not a claim that the
route is protected.

**Body shapes on the streaming turn route** (`POST /api/sessions/{id}/turn/stream`, JSON model):

| Request shape | Browser can send it cross-site without preflight? | Result |
| --- | --- | --- |
| `text/plain` (string body from `fetch` or `sendBeacon`) | yes | 422, refused before the handler |
| `application/x-www-form-urlencoded` (HTML form) | yes | 422 |
| `multipart/form-data` | yes | 422 |
| no `Content-Type` (for example `fetch` with a Blob of empty type) | yes | 422 |
| `application/json` | no, needs a preflight | reaches the handler (blocked by the default policy when cross-site) |

So the JSON turn route is not reachable through a simple request. Other JSON routes use the same FastAPI
body parsing, but each was not executed separately.

**Mutating routes reachable by a simple request.** Of 39 mutating routes, 9 accept a cross-site simple
request with no body check, and CORS does not stop them in local mode:

| Method and path | Body |
| --- | --- |
| `POST /api/adventures/{slug}/campaign` | none |
| `POST /api/adventures/{slug}/cancel` | none |
| `POST /api/adventures/{slug}/manifest/compile` | none |
| `POST /api/campaigns/{campaign_id}/end` | none |
| `POST /api/campaigns/{campaign_id}/reopen` | none |
| `POST /api/seed` | none (creates seed user and campaign rows if missing) |
| `POST /api/sessions/{session_id}/intro` | none (starts generation) |
| `POST /api/adventures/upload` | multipart file |
| `POST /api/campaigns/{campaign_id}/sources/{slug}/upload` | multipart file |

A minimal app test (`test_simple_form_post_reaches_a_bodyless_route_without_a_content_type_check`) shows
that FastAPI does not check `Content-Type` on a bodyless route, so a cross-site HTML form runs the
handler. The real handlers were not executed in tests because they write data.

The other 30 mutating routes are either `DELETE` (7, which needs a preflight that the default policy
refuses) or JSON-body `PATCH`, `PUT` and `POST` (23, which need a preflight). The test
`test_every_mutating_route_is_classified_in_the_request_mode_table` pins this classification and fails
when a route is added without a deliberate placement.

**Consequence.** In local mode without authentication, a cross-site page can trigger these nine
routes. The effects include creating or changing stored campaign, adventure and seed rows, and
starting model work (the session intro, which generates an opening scene). Local mode
is therefore only safe on a trusted network. Do not expose it publicly until authentication (EDG-065)
protects these routes.

## What is not done

- No TLS termination. Serve the API and UI over HTTPS in front of it.
- No cookie sessions, no SameSite configuration, and no CSRF tokens. None is needed while the client
  sends no cookies. Revisit if cookie auth is added.
- No Origin or Referer check on unsafe methods. The 9 simple-request routes above remain reachable in
  local mode. Adding this check changes default behaviour for same-origin proxies whose `Host` header
  differs from the public origin, so it is a follow-up decision, not part of this change.
- IPv6 literals are not accepted in `EDGAR_TRUSTED_HOSTS`.
- Unhandled 500s carry no CORS headers, because the outermost error handler runs outside the middleware.
  A cross-origin client sees a network error there, not the error body.
- No compose or Dockerfile changes. The compose API allow-list must include the three new names before
  `tests/test_compose_hardening.py` can pass after PR #91 merges.

## Checklist for a separately hosted UI

- [ ] The UI origin is set exactly in `EDGAR_ALLOWED_ORIGINS`. It has scheme, host and optional
      non-default port, no path, and no trailing slash.
- [ ] The UI calls the API with `Authorization: Bearer ...` (bearer mode), not cookies.
- [ ] `EDGAR_ALLOW_CREDENTIALS` is `0` (or unset).
- [ ] `EDGAR_TRUSTED_HOSTS` lists the API's public host name if you set it, plus any local probe host.
- [ ] Bearer mode is on (EDG-065) before the API is reachable from anywhere other than a trusted network.
- [ ] `curl -i -H 'Origin: https://ui.example.com' ...` shows `Access-Control-Allow-Origin` for the UI
      and nothing for any other origin.
- [ ] The API is served over HTTPS.
