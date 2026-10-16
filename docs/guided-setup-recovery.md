# Guided setup retries

The guided setup retains its adventure/campaign, hero, assignment and save IDs in browser
storage. Back navigation and retries resume that attempt. A deliberate fresh playthrough
gets a new attempt. If browser storage cannot retain the pending creation key/body, the UI
keeps same-tab progress and blocks the create request with actionable copy until storage
works. This avoids losing the only retry identity on reload. Repair and source review use the server's existing readiness result.

`POST /api/characters` and `POST /api/sessions` accept an optional UUID `request_id`. The UI
retains one key and the exact original request body at each creation boundary until its
outcome is known. A keyed retry returns the existing record even after later edits, without
changing its current state. Reusing the key with a different original payload returns HTTP
409. Keys are scoped to the character owner or save campaign. Unkeyed calls retain their
independent-create behavior.

The original canonical payload digest and key are committed on the created row. A PostgreSQL
transaction advisory lock serializes lookup and creation for each scoped key, backed by a
unique constraint. Migration `16_setup_creation_requests` adds nullable identity columns;
existing data retains null identities. Downgrade removes retry identities while retaining
heroes, saves, histories and assignments. Apply migrations before serving updated routes.

A request ID is an idempotency identity, not an authentication token. Existing access,
readiness and assignment guards remain authoritative. Creating another save does not reset
the prior save or silently move a hero out of another active campaign. No navigation action
automatically retries paid adventure preparation.

## Verification and remaining acceptance

Root review added durable-storage failure guards, strict persisted UUID/body validation,
route-ID isolation, assignment-error recovery, exact standalone hero retries and preference
for a played save over a newer empty save. Mounted tests exercise ambiguous hero/save
responses across remount, changed presets, lost assignment responses, conflicting assignments,
missing hero lookups, storage failures and route changes. The final combined local suite
passed 233 frontend tests, ESLint, TypeScript/Vite production build, Ruff and five pure setup
request tests. PostgreSQL replay/concurrency and migration upgrade/downgrade checks run in
unchanged GitHub CI; they were not run against local containers or live data.

Disposable GET-only browser fixtures exercised six similar long-title library cards,
campaign details, hero choice/create and retained-save setup at 320, 390 and 1280 CSS pixels.
Chrome 155.0.8059.82 showed no unexpected overflow, JavaScript errors or API writes after
review fixes. Full title expansion and Continue to the played save were exercised. Only
session-earned journal place/goal is requested for readable histories; private preparation
is not used. The browser evidence is a seeded walkthrough, not a human task study.

EDG-042 remains awaiting the brief's EDG-055 first-session and returning-player human task
observations after merge. EDG-040's actual phone keyboard, touch and device/AT criteria remain
separate from desktop viewport emulation. No live generation, provider calls or deployment
were performed for this work.
