# Guided setup retries

The guided setup retains its adventure/campaign, hero, assignment and save IDs in browser
storage. Back navigation and retries resume that attempt. A deliberate fresh playthrough
gets a new attempt. Repair and source review use the server's existing readiness result.

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
